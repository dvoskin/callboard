"""Paid retainers, honored in the CRM.

Danny, 2026-10-19: "Getting complaints that some patients who pay their
retainer invoices on Zoho, don't have their corresponding deals move to the
correct stage" / "Fix them all fully. Make sure it doesn't happen again too".

When a retainer invoice is paid, Books calls the CRM function
paymentmadeupdatedeal, which ticks Retainer_Paid and moves the deal to
"Payment Received, Needs Surgery Scheduled"; that stage fires "Assign Surgical
Coordinator". Measured from 2026-04-01 to 2026-10-19 (2,803 retainer payments),
three things left a paying patient with no coordinator:

  1. The call never came. 19 payments, 17 of them between 2026-08-24 and
     2026-09-28, 12 of those through Authorize.Net. The deal stayed in
     Retainer Invoice Sent and kept getting sales follow-ups.
  2. "Deduplication of Deals When Created" (CDD_Deals_Deduplication_By_Phone)
     merges a person's older deals INTO the newly created one and deletes them.
     When the new one is a bare intake or Instagram deal, the paid deal is the
     one deleted: 240 paid deals since June. Its full record survives only in
     the "Merged duplicate:" note it leaves on the survivor.
  3. A STOP reply (detect_sms_stop, st_backfill_auto) moves the deal to
     Unsubscribe whatever its stage, paid included, which takes it off every
     coordinator queue.
  4. The stage moved but nobody got the patient. dealSchedulingAssign's round
     robin still held Ana Castro, disabled on 2026-10-07: 22 paid patients from
     Oct 6 to Oct 19 got the "Meet your coordinator" email and no coordinator,
     no intro call. Before 10 AM she was the only one "on shift", so every
     early payment went to her.

Every fifteen minutes this module checks the four, one record at a time:

  decide_payment(p, deal, handled, now)   a Books retainer payment
  decide_payment_merged(p, snapshot, survivor, handled, now)
                                          one whose deal was already merged away
  decide_merge(snapshot, survivor, handled)  a "Merged duplicate:" note
  decide_unsub(deal, handled, now)        a paid deal sitting on Unsubscribe
  decide_owner(deal, schedules, active, load, local_now, now)
                                          a paid deal with no working coordinator
  decide_intro(deal, contact, sender, now, on_now)
                                          the coordinator's intro text

and returns the exact write, or "review" for a person.

Rules
  - Mirror the real flow. The fix is the write paymentmadeupdatedeal makes:
    Retainer_Paid, Retainer_Paid_Date (the Books payment time, not now) and the
    stage "Stage Changes on Checkbox" would pick -- Goals Financing Program when
    Payment_Method says Goals Financing, else Payment Received. Sent WITH
    workflows, so the coordinator is assigned exactly as on a normal payment.
  - Never over a coordinator. A patient with an Active Journey, a Journey or a
    paid deal newer than this payment, is already handled: no stage move (the
    stale deal gets the box only, sent without workflows). A Journey under the
    same name on another contact record holds the move for a person.
  - Never a stage nobody listed. Moves only out of MOVE_STAGES; a paid stage
    with the box unticked gets the box only, silently; everything else
    (ON HOLD, DND, Cancelled or Disputed, Duplicates / Junk, an unknown stage)
    is "review".
  - Leave the normal flow its chance: a payment younger than GRACE_MINUTES is
    not looked at yet.
  - Opt-outs stay. A deal brought back from Unsubscribe keeps RC_SMS_Opt_Out and
    the contact's flags; only its stage changes.
  - A coordinator from the CRM's own Team Schedules (Team Coordinator, Active,
    not out, an active CRM user), the one on shift now with the fewest
    patients waiting -- or, with nobody on shift, the next one in. Only after
    COORD_GRACE_MINUTES, so dealSchedulingAssign always has the first go. The
    intro call task is named "Intro Call - ..." so completing it fires
    "Intro Call Completed" and the Journey is made as usual.
  - One intro text per paid patient, from their own coordinator's line,
    through SMS Outbox (Danny, 2026-10-19: "have an intro message go out from
    the coordinators instead of having them dial all of it"). Never to a deal
    or contact that opted out of texts or is Do Not Contact; 9 AM to 7 PM in
    the patient's time zone, never on Sunday, and only while the coordinator
    is on shift to read the reply; only while the quoted date is at least
    two weeks ahead -- a passed or close date is a conversation, not a text.
    A deal tagged "No Intro Text" or "Do not Call" is never texted. Before a
    coordinator's first intro text of the day, the same line texts the owner
    a copy marked SAMPLE, without the patient's name (PAID_GUARD_INTRO_SAMPLE_TO;
    Danny, 2026-10-19: "Include my number ... as a sample in each
    coordinators blast to verify it sent"); a sample that does not go holds
    that line's texts.

Names and numbers stay out of the logs.
"""
from __future__ import annotations

import json
import os
import re
import threading
import zoneinfo
from datetime import date, datetime, timedelta

PAYMENT_RECEIVED = "Payment Received, Needs Surgery Scheduled"
GOALS_FINANCING = "Goals Financing Program"
UNSUB = "Unsubscribe"

# The deposit is in. Closed Won stages included: a deal there is past the
# coordinator hand-off.
PAID_STAGES = frozenset({
    PAYMENT_RECEIVED, GOALS_FINANCING, "Manager Support Needed",
    "Closed Won - Surgery Scheduled", "Partial Deposit - No Date", "Payment Completed", "Order Fulfilled",
    "Payment Complete — Pending Manual Review", "Final Review Needed", "Final Review Approved",
})
# Paid but before the Journey exists: the state a merge or a STOP strands.
PRE_JOURNEY = frozenset({PAYMENT_RECEIVED, GOALS_FINANCING, "Manager Support Needed"})
# A paid deal in one of these moves to the paid stage. An allowlist: a stage
# nobody listed here goes to review.
# Checked against the Deals Stage picklist on 2026-10-19. Left out on purpose:
# ON HOLD and DND (a person put it there -- Shylasia Capehart paid in full and
# stays ON HOLD), Lost / Closed, Cancelled or Disputed, Duplicates / Junk and
# the other product lines (Folli, Lumelan, hair, weight loss).
MOVE_STAGES = frozenset({
    "New Deal", "New Deal / Instagram", "Meta @", "Intake Form Completed", "Call Scheduled",
    "Call Scheduled / Assigned", "No Show", "Not Reached", "Attempted FU Need 1", "Attempted FU Need 2",
    "Attempted FU Need 3", "Consultation Completed", "Qualified", "Quote Sent", "Quote Accepted", "Follow Up",
    "Postive Response", "Negative Response", "In Progress", "Contract Sent", "Contract Signed",
    "Retainer Invoice Sent", "Invoice Sent", "Pending Action", "Expired", "Old Leads", UNSUB, "WhatsApp",
})

GRACE_MINUTES = 30
# A paid deal with no working coordinator this long after it was paid gets one.
COORD_GRACE_MINUTES = 30
# A paid deal on Unsubscribe is brought back only when the payment is this
# recent. An older one is a past patient who opted out of marketing.
RECENT_DAYS = 180

DEAL_FIELDS = ("id, Deal_Name, Stage, Retainer_Paid, Retainer_Paid_Date, Retainer_Invoice_URL, Payment_Method, "
               "Estimate_Sent, Contact_Name, Scheduling_Owner, Zoho_Books_Customer_ID, Tentative_Procedure_Date, "
               "Location, Surgeon, RC_SMS_Opt_Out, Tag, Created_Time, Modified_Time")
INTRO_CONTACT_FIELDS = "id, First_Name, Phone, Mobile, Do_Not_Contact, ringcentralextensionabr__SMS_OPT_OUT"
JOURNEY_FIELDS = "id, Name, Case_Status, Journey_Stage, Contact, Deal, Created_Time"

# Copied from a merge snapshot onto a survivor that has no quote of its own.
QUOTE_FIELDS = (
    "Deal_Name", "Amount", "Procedure", "Procedure_Description", "Tentative_Procedure_Date", "Surgeon", "Location",
    "Payment_Method", "Financing_Provider", "Total_Cost", "Balance", "Processing_Fee", "Post_Op_Care_Package",
    "Post_Op_Cost", "Post_Op_Package_Description", "Anesthesia_Type", "Anesthesia_Cost", "Supply_and_Consumable_Cost",
    "Total_Amount_of_surgery", "Promo_Name", "Promos_Description", "Promotional_Discounts", "Full_Non_Promo_Price",
    "Includes_Promo_Discounts", "Add_ons_Description", "Total_Add_Ons_Cost", "Extras_Description", "Total_Extras_Cost",
    "Estimate_Sent", "Quote_Sent_Date", "Estimate_URL", "Consultation_Completed", "Consultation_Date", "Height_ft",
    "Height_in", "Weight_lbs", "BMI_Max_by_Surgery_Date",
)
# Copied whatever the survivor holds: they are the payment itself.
PAYMENT_FIELDS = ("Retainer_Sent", "Retainer_Sent_Date", "Retainer_Invoice_URL", "Invoice_URL", "Zoho_Books_Customer_ID")


def _t(s):
    """ISO datetime -> aware datetime; Books' "-0400" and Zoho's "-04:00" both. None when blank or unreadable."""
    if not s:
        return None
    s = str(s)
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def _id(ref):
    return (ref or {}).get("id") if isinstance(ref, dict) else None


def retainer_id(url) -> str:
    m = re.search(r"retainerinvoices/(\d+)", str(url or ""))
    return m.group(1) if m else ""


def target_stage(payment_method) -> str:
    """The stage "Stage Changes on Checkbox" picks for a paid deal."""
    return GOALS_FINANCING if "goals financing" in str(payment_method or "").lower() else PAYMENT_RECEIVED


def is_retainer_payment(p) -> bool:
    return p.get("payment_type") == "Retainer Payment" and bool(p.get("retainerinvoice_id"))


def skip_reason(p):
    """A retainer payment this module must not act on, or None."""
    try:
        if float(p.get("bcy_refunded_amount") or 0) > 0:
            return "refunded"
    except (TypeError, ValueError):
        pass
    name = str(p.get("customer_name") or "").lower()
    if "training" in name or str(p.get("description") or "").strip().lower() == "test":
        return "test"
    return None


def paid_at(p):
    """When the payment landed: Books' last_modified_time, which is what the
    real flow stamps (a backdated entry keeps its true entry time)."""
    return _t(p.get("last_modified_time")) or _t(p.get("created_time"))


def handled(contact_ids, deals, journeys, since, exclude_deal=None) -> bool:
    """Is a coordinator already on this patient for THIS payment?

    An Active Journey, a Journey created since the payment (minus a week), or
    another deal paid since then. An earlier purchase does not count: India
    Smith had a 2023 Journey and her 2026 deposit was still stranded.
    A Journey is the patient's when its Contact is theirs or its Deal is one of
    theirs: the contact merge leaves some Journeys with no Contact at all.
    deals: [deal rows]; journeys: [journey rows with Contact, Deal, Case_Status, Created_Time]."""
    cids = {c for c in contact_ids if c}
    dids = {d.get("id") for d in deals} | {exclude_deal}
    dids.discard(None)
    floor = since - timedelta(days=7) if since else None
    for j in journeys:
        if _id(j.get("Contact")) not in cids and _id(j.get("Deal")) not in dids:
            continue
        if j.get("Case_Status") == "Active":
            return True
        created = _t(j.get("Created_Time"))
        if floor and created and created >= floor:
            return True
    for d in deals:
        if d.get("id") == exclude_deal or _id(d.get("Contact_Name")) not in cids:
            continue
        if d.get("Stage") not in PAID_STAGES:
            continue
        when = _t(d.get("Retainer_Paid_Date")) or _t(d.get("Created_Time"))
        if not floor or (when and when >= floor):
            return True
    return False


def _words(name):
    return sorted(w for w in re.split(r"[\s,]+", str(name or "").lower()) if w)


def same_name(journeys, name, since) -> bool:
    """A Journey under the patient's exact name, in either order ("Walters
    Taneika" is Taneika Walters), Active or begun since the payment (minus a
    week). Shicresia Metoyer's coordinator was on a second contact record with
    no email or phone to tie the two. A name is not proof: a match sends the
    deal to a person instead of moving it."""
    want = _words(name)
    if len(want) < 2 or want == ["unknown"]:
        return False
    floor = since - timedelta(days=7) if since else None
    for j in journeys:
        if _words(j.get("Name")) != want:
            continue
        created = _t(j.get("Created_Time"))
        if j.get("Case_Status") == "Active" or (floor and created and created >= floor):
            return True
    return False


def hold_for_name(dec, by_name):
    """A stage move held for a person when only a same-name Journey says the
    patient may already be with a coordinator."""
    if dec["result"] == "fix" and by_name:
        return _decision("review", "a Journey under the same name on another contact", dec["deal"],
                         old=dec.get("old"))
    return dec


def _decision(result, reason, deal_id=None, write=None, trigger=None, note=None, **kw):
    d = {"result": result, "reason": reason, "deal": deal_id, "write": write or {}, "trigger": trigger,
         "note": note}
    d.update(kw)
    return d


def _gate(p, now):
    """skip / wait for a payment, or None to go on."""
    why = skip_reason(p)
    if why:
        return _decision("skip", why)
    at = paid_at(p)
    if at and now - at < timedelta(minutes=GRACE_MINUTES):
        return _decision("wait", "inside the grace period")
    return None


def _line(p):
    return "Books retainer payment #%s, $%s on %s" % (p.get("payment_number"), p.get("amount"), p.get("date"))


def pick_deal(hits):
    """The deal a retainer belongs to, from every deal whose Retainer_Invoice_URL
    names it: (deal, None), or (None, reason) when it cannot be told. A paid
    one wins -- the payment is already recorded there."""
    if len(hits) == 1:
        return hits[0], None
    paid = [h for h in hits if h.get("Retainer_Paid") and h.get("Stage") in PAID_STAGES]
    if paid:
        return paid[0], None
    if hits:
        return None, "%d deals carry this retainer" % len(hits)
    return None, None


def decide_payment(p, deal, is_handled, now, candidate=None):
    """One Books retainer payment against the deal its retainer belongs to.
    candidate: with no deal, a deal of the same Books customer, shown to the
    person who reviews it -- never written."""
    gate = _gate(p, now)
    if gate:
        return gate
    if not deal:
        return _decision("review", "no CRM deal carries this retainer", candidate)
    at = paid_at(p)
    stage = deal.get("Stage") or ""
    paid = {"Retainer_Paid": True, "Retainer_Paid_Date": _iso(at)}
    if stage in PAID_STAGES:
        if deal.get("Retainer_Paid"):
            return _decision("ok", "", deal["id"])
        return _decision("flag", "paid stage, box unticked", deal["id"], paid, [],
                         "%s. The stage was already paid; Retainer Paid was unticked and is now set." % _line(p))
    ticked = bool(deal.get("Retainer_Paid"))
    if is_handled:
        if ticked:
            return _decision("ok", "already with a coordinator", deal["id"])
        # Priscilla Williams: the payment landed on an old deal while a newer
        # one already had her coordinator. The box records it; the stage stays.
        return _decision("flag", "already with a coordinator on another deal", deal["id"], paid, [],
                         "%s was not reflected on this deal (stage %s). The patient is already with a coordinator, "
                         "so only Retainer Paid was set; the stage was left as it was." % (_line(p), stage))
    if stage not in MOVE_STAGES:
        return _decision("review", "stage " + stage, deal["id"])
    to = target_stage(deal.get("Payment_Method"))
    if ticked:
        # The payment did land, then the deal left the paid stage: a STOP
        # reply puts it on Unsubscribe. The paid date already there stays.
        return _decision("fix", "paid deal off the paid stage", deal["id"], {"Stage": to}, ["workflow"],
                         "%s. The deal was marked paid but sat in %s; moved to %s so a coordinator is assigned."
                         % (_line(p), stage, to))
    return _decision("fix", "payment never reached the deal", deal["id"], dict(paid, Stage=to), ["workflow"],
                     "%s was not reflected on this deal (stage was %s). Retainer Paid set and the deal moved to "
                     "%s so a coordinator is assigned." % (_line(p), stage, to))


def decide_payment_merged(p, snap, survivor, is_handled, now):
    """A payment whose deal the dedup had already deleted: the retainer is
    found only in a "Merged duplicate:" note. Treated as that deal, paid,
    merged into the survivor."""
    gate = _gate(p, now)
    if gate:
        return gate
    if not snap:
        return _decision("review", "no CRM deal carries this retainer")
    stage = snap.get("Stage") or ""
    paid = dict(snap, Retainer_Paid=True, Retainer_Paid_Date=_iso(paid_at(p)))
    if stage in MOVE_STAGES:
        paid["Stage"] = target_stage(snap.get("Payment_Method"))
    elif stage not in PRE_JOURNEY:
        return _decision("review", "merged deal was in stage " + stage, _id(survivor) or None, old=snap.get("id"))
    d = decide_merge(paid, survivor, is_handled)
    if d["result"] == "fix":
        d["note"] = "%s. %s" % (_line(p), d["note"])
    return d


def parse_snapshot(note_content):
    """The deleted deal's full record, from a "Merged duplicate:" note. None
    when the note carries none (raw newlines inside it are allowed)."""
    c = str(note_content or "")
    i = c.find("FULL RECORD JSON:")
    if i < 0:
        return None
    try:
        rec = json.loads(c[i + len("FULL RECORD JSON:"):].strip(), strict=False)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _copy(src, keys):
    out = {}
    for k in keys:
        v = src.get(k)
        if v is None or v == "" or v == []:
            continue
        out[k] = {"id": v["id"]} if isinstance(v, dict) and "id" in v else v
    return out


def _waiting(snap, now):
    """Paid and still waiting for a coordinator: before the Journey, or on
    Unsubscribe after a STOP with a recent payment (45 of the 240 deleted paid
    deals were on Unsubscribe)."""
    if not snap.get("Retainer_Paid"):
        return False
    if snap.get("Stage") in PRE_JOURNEY:
        return True
    at = _t(snap.get("Retainer_Paid_Date"))
    return bool(now and snap.get("Stage") == UNSUB and at and now - at <= timedelta(days=RECENT_DAYS))


def decide_merge(snap, survivor, is_handled, now=None):
    """A deal the dedup merged away. Acts only when it was paid and waiting for
    a coordinator; a merged Closed Won deal already has its Journey."""
    if not snap:
        return _decision("skip", "no snapshot")
    if not _waiting(snap, now):
        return _decision("skip", "not a paid deal waiting for a coordinator")
    if not survivor:
        return _decision("review", "the deal it was merged into is gone too", None, old=snap.get("id"))
    stage = survivor.get("Stage") or ""
    if stage in PAID_STAGES or is_handled:
        return _decision("ok", "", survivor["id"], old=snap.get("id"))
    if stage not in MOVE_STAGES:
        return _decision("review", "merged into a deal in stage " + stage, survivor["id"], old=snap.get("id"))
    write = _copy(snap, PAYMENT_FIELDS)
    # The survivor's own quote wins: a rep who re-quoted after the merge meant it.
    own_quote = bool(survivor.get("Estimate_Sent")) or bool(survivor.get("Retainer_Invoice_URL"))
    if not own_quote:
        write.update(_copy(snap, QUOTE_FIELDS))
    if snap.get("Scheduling_Owner") and not survivor.get("Scheduling_Owner"):
        write["Scheduling_Owner"] = {"id": _id(snap["Scheduling_Owner"])}
    to = target_stage(snap.get("Payment_Method") or survivor.get("Payment_Method"))
    write.update({"Retainer_Paid": True, "Retainer_Paid_Date": snap.get("Retainer_Paid_Date"), "Stage": to})
    return _decision("fix", "paid deal merged into this one", survivor["id"], write, ["workflow"],
                     "The paid deal %s (%s, stage %s) was deleted by the duplicate merge and folded into this deal. "
                     "Payment%s restored here and the deal moved to %s so a coordinator is assigned. The full original "
                     "is in the 'Merged duplicate' note." % (snap.get("id"), snap.get("Deal_Name"), snap.get("Stage"),
                                                            "" if own_quote else ", quote", to),
                     old=snap.get("id"))


def decide_unsub(deal, is_handled, now):
    """A paid deal on Unsubscribe: opted out of texts, still a patient."""
    if deal.get("Stage") != UNSUB or not deal.get("Retainer_Paid"):
        return _decision("skip", "not a paid deal on Unsubscribe", deal.get("id"))
    at = _t(deal.get("Retainer_Paid_Date"))
    if not at or now - at > timedelta(days=RECENT_DAYS):
        return _decision("skip", "an old payment", deal["id"])
    if is_handled:
        return _decision("ok", "", deal["id"])
    to = target_stage(deal.get("Payment_Method"))
    return _decision("fix", "paid deal moved to Unsubscribe", deal["id"], {"Stage": to}, ["workflow"],
                     "This deal was paid on %s and then moved to Unsubscribe (a STOP reply). A paid patient stays in the "
                     "coordinator pipeline: moved to %s. The text opt-out stays in place." % (at.date().isoformat(), to))

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hours(sched, day):
    """(start, end) hours a Team Schedules row works on `day` ("Mon".."Sun"),
    or None when that is a day off. Saturday_Hours / Sunday_Hours ("10-17")
    override the weekday hours on those days."""
    if day in (sched.get("Days_Off") or []):
        return None
    special = {"Sat": sched.get("Saturday_Hours"), "Sun": sched.get("Sunday_Hours")}.get(day)
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", str(special or ""))
    if m:
        return int(m.group(1)), int(m.group(2))
    try:
        return int(sched.get("Start_Hour")), int(sched.get("End_Hour"))
    except (TypeError, ValueError):
        return None


def _out(sched, day_iso):
    a, b = sched.get("Out_From"), sched.get("Out_To")
    return bool(a) and a <= day_iso and (not b or day_iso <= b)


def next_shift(sched, local_now, horizon=8):
    """The local date the coordinator is next working at or after local_now
    (today while the shift has not ended), or None within `horizon` days."""
    for i in range(horizon):
        d = (local_now + timedelta(days=i)).date()
        h = _hours(sched, _DAYS[d.weekday()])
        if not h or _out(sched, d.isoformat()):
            continue
        if i == 0 and local_now.hour >= h[1]:
            continue
        return d
    return None


def on_shift(sched, local_now):
    """True while the coordinator is working right now (their hours today,
    not out)."""
    h = _hours(sched, _DAYS[local_now.weekday()])
    return bool(h) and not _out(sched, local_now.date().isoformat()) and h[0] <= local_now.hour < h[1]


def pick_coordinator(schedules, active, load, local_now):
    """(user id, name, due date) of the coordinator a stranded patient goes to:
    on shift now first, else the next one in (earliest day, then earliest
    start), then the fewest patients waiting, then the name. None when nobody
    qualifies."""
    best = None
    for s in schedules:
        uid = _id(s.get("Staff_User"))
        if s.get("Team") != "Coordinator" or not s.get("Active") or not uid or uid not in active:
            continue
        day = next_shift(s, local_now)
        if not day:
            continue
        on_now = on_shift(s, local_now)
        start = 0 if on_now else (_hours(s, _DAYS[day.weekday()]) or (0, 0))[0]
        key = (0 if on_now else 1, day, start, load.get(uid, 0), str(s.get("Name") or ""))
        if best is None or key < best[0]:
            best = (key, uid, str(s.get("Name") or uid), day)
    return (best[1], best[2], best[3]) if best else None


def decide_owner(deal, schedules, active, load, local_now, now):
    """A paid deal in Payment Received with no coordinator, or one whose
    coordinator is no longer an active CRM user. active: the set of active
    CRM user ids -- empty when it could not be read, and then a deal that
    names a coordinator is left alone."""
    if deal.get("Stage") != PAYMENT_RECEIVED or not deal.get("Retainer_Paid"):
        return _decision("skip", "not a paid deal waiting for scheduling", deal.get("id"))
    owner = _id(deal.get("Scheduling_Owner"))
    if owner and (not active or owner in active):
        return _decision("ok", "", deal["id"])
    since = _t(deal.get("Retainer_Paid_Date")) or _t(deal.get("Modified_Time"))
    if since and now - since < timedelta(minutes=COORD_GRACE_MINUTES):
        return _decision("wait", "inside the grace period", deal["id"])
    pick = pick_coordinator(schedules, active, load, local_now)
    if not pick:
        return _decision("review", "no coordinator on the Team Schedules to assign", deal["id"])
    uid, cname, due = pick
    patient = str((deal.get("Contact_Name") or {}).get("name") or "the patient")
    try:
        tent = date.fromisoformat(str(deal.get("Tentative_Procedure_Date") or ""))
    except ValueError:
        tent = None
    short = "%s %d" % (tent.strftime("%b"), tent.day) if tent else ""
    why = ("its coordinator is no longer an active CRM user" if owner else "the automatic assignment left it without one")
    flags = []
    if tent and tent < local_now.date():
        flags.append("- Tentative date %s is in the past: agree a new date with the patient" % short)
    task = {
        # "Intro Call - ..." is what the Tasks rule "Intro Call Completed" looks for.
        "Subject": "Intro Call - %s" % patient + (" \u00b7 Deal %s" % short if short else ""),
        "Owner": {"id": uid}, "Due_Date": due.isoformat(), "Status": "Not Started", "Priority": "High",
        "What_Id": {"id": deal["id"]}, "$se_module": "Deals",
        "Description": ("SCHEDULING CALL - follow the New Deals Scheduling Protocol.\n"
                        "WHY YOU HAVE THIS: paid, but %s, so nobody had the patient. Assigned by the paid-retainer check.\n"
                        "RED FLAGS:\n%s\n\n"
                        "1. BEFORE CALLING review the whole deal: patient info, procedure, surgeon, location, tentative date "
                        "(must be inside the promo deadline), price + promotion, deposit/payment, invoice, extra fees, special notes.\n"
                        "2. ANY ISSUE: DO NOT BOOK. Move the Deal to Manager Support Needed and escalate to Daniel Voskin.\n"
                        "3. ALL CORRECT: book surgery + pre-op + post-op in DrChrono, allocate the payment, explain payment "
                        "deadlines and photo / ID / labs / medical clearance requirements, send the surgery info.\n"
                        "4. NO ANSWER: if the deal is fully correct, book on the invoice date anyway, send the surgery info and log the attempt.\n"
                        "WHEN DONE: put the booked date in Surgery Date/Time on the Deal, then mark this task Completed. "
                        "That creates the patient's Journey with YOU as the owner.\nDeal: %s")
                       % (why, "\n".join(flags) if flags else "- none found", deal.get("Deal_Name") or ""),
    }
    return _decision("fix", "no working coordinator", deal["id"], {"Scheduling_Owner": {"id": uid}}, [],
                     "Paid, but %s. Assigned to %s with an intro call task due %s." % (why, cname, due.isoformat()),
                     task=task, to=uid)


# ---- the coordinator's intro text ----------------------------------------------
INTRO_TEMPLATE = "coordinator-intro-v1"
INTRO_SAMPLE_TEMPLATE = "coordinator-intro-v1-sample"
INTRO_HOURS = (9, 19)           # patient's local time; Sunday never
INTRO_MIN_LEAD_DAYS = 14        # a quoted date closer than this is for a call, not a "reply YES"
INTRO_STOP_TAGS = ("No Intro Text", "Do not Call")  # a deal tagged either is never texted
_TZ = {"Houston": "America/Chicago", "Los Angeles": "America/Los_Angeles"}
# Where each surgeon actually operates (DrChrono, May 2026 on). A deal naming
# a surgeon at an office they never work at gets neither in the text.
SURGEON_CITIES = {
    "Nguyen": {"Houston"}, "Narh-Martey": {"Atlanta"}, "Zemlyak": {"Atlanta"}, "Johnson": {"the Bronx"},
    "Perkins": {"the Bronx", "Brooklyn", "New Jersey", "New York"}, "Hsu": {"Los Angeles", "the Bronx", "Brooklyn", "New York"},
    "Erwin": {"New Jersey", "the Bronx"}, "Douyon": {"New Jersey", "the Bronx"}, "Stafford": {"Los Angeles"},
    "Morrow": {"Maryland"}, "Iskander": {"Maryland"}, "Voskin": {"Philadelphia", "the Bronx", "Atlanta"},
}


def city(location):
    """A deal's free-text Location as the patient would say it, or ""."""
    l = str(location or "").strip().lower()
    for keys, name in ((("bronx", "bx"), "the Bronx"), (("houston", "texas", "tx"), "Houston"), (("atlanta", "atl", "ga"), "Atlanta"),
                       (("brooklyn",), "Brooklyn"), (("philadelphia", "philly"), "Philadelphia"), (("los angeles", "la", "beverly"), "Los Angeles"),
                       (("new jersey", "nj", "ridgefield"), "New Jersey"), (("maryland", "md", "gaithersburg"), "Maryland"),
                       (("miami",), "Miami"), (("new york", "ny", "harlem", "manhattan"), "New York")):
        if any(re.search(r"\b%s\b" % re.escape(k), l) for k in keys):
            return name
    return ""


def surgeon_short(surgeon):
    """'Dr. Phuong X. Nguyen' -> 'Dr. Nguyen'; '' when none."""
    words = [w for w in re.split(r"\s+", str(surgeon or "").strip()) if w and w.lower() not in ("dr", "dr.")]
    return "Dr. " + words[-1] if words else ""


def phone10(*values):
    for v in values:
        d = re.sub(r"\D", "", str(v or ""))
        if len(d) == 11 and d[0] == "1":
            d = d[1:]
        if len(d) == 10 and d[0] not in "01":
            return d
    return ""


def intro_text(first, coordinator, tent, surgeon, where):
    """The text itself. tent: a date in the future."""
    doc = surgeon_short(surgeon)
    last = doc.split()[-1] if doc else ""
    if doc and where and last in SURGEON_CITIES and where not in SURGEON_CITIES[last]:
        doc, where = "", ""                       # the deal contradicts itself: say neither
    with_ = (" with %s" % doc if doc else "") + (" in %s" % where if where else "")
    hi = ("Hi %s, " % first) if first else "Hi, "
    return ("%sthis is %s from GOALS Plastic Surgery, your surgical coordinator. Thank you for your deposit! "
            "Your quote has you down for %s %d%s. Reply YES to lock in that date, or reply with a better week "
            "and I'll find you a spot. Reply STOP to opt out." % (hi, coordinator, tent.strftime("%b"), tent.day, with_))


def decide_intro(deal, contact, sender, now, on_now=True):
    """One coordinator intro text for a paid deal waiting in Payment Received.
    sender: the coordinator's SMS Outbox Sender ("Judith") -- None when their
    line is not set up yet. on_now: the coordinator is on shift, so a reply
    reaches someone. contact: the deal's Contact row."""
    if deal.get("Stage") != PAYMENT_RECEIVED or not deal.get("Retainer_Paid"):
        return _decision("skip", "not a paid deal waiting for scheduling", deal.get("id"))
    if not sender:
        return _decision("hold", "coordinator has no texting line set up", deal["id"])
    contact = contact or {}
    if (deal.get("RC_SMS_Opt_Out") or contact.get("ringcentralextensionabr__SMS_OPT_OUT")
            or contact.get("Do_Not_Contact")):
        return _decision("skip", "opted out or Do Not Contact", deal["id"])
    stop = {x.lower() for x in INTRO_STOP_TAGS}
    tagged = [str((t or {}).get("name") or "").strip() for t in deal.get("Tag") or []]
    if any(t.lower() in stop for t in tagged):
        return _decision("skip", "tagged %s" % next(t for t in tagged if t.lower() in stop), deal["id"])
    phone = phone10(contact.get("Mobile"), contact.get("Phone"))
    if not phone:
        return _decision("skip", "no phone", deal["id"])
    where = city(deal.get("Location"))
    tz = zoneinfo.ZoneInfo(_TZ.get(where, "America/New_York"))
    local = now.astimezone(tz)
    try:
        tent = date.fromisoformat(str(deal.get("Tentative_Procedure_Date") or ""))
    except ValueError:
        tent = None
    if not tent or tent <= local.date():
        return _decision("hold", "quoted date passed or missing", deal["id"])
    if (tent - local.date()).days < INTRO_MIN_LEAD_DAYS:
        return _decision("hold", "quoted date too close to text -- the intro call covers it", deal["id"])
    if not on_now:
        return _decision("wait", "coordinator not on shift", deal["id"])
    if local.weekday() == 6 or not (INTRO_HOURS[0] <= local.hour < INTRO_HOURS[1]):
        return _decision("wait", "outside texting hours", deal["id"])
    first = str(contact.get("First_Name") or "").strip()
    if first.lower() == "unknown" or len(first) < 2:
        first = ""
    text = intro_text(first.title() if first.isupper() or first.islower() else first, sender, tent,
                      deal.get("Surgeon"), where)
    row = {"Name": "Intro - %s" % (str((deal.get("Contact_Name") or {}).get("name") or "patient"))[:110],
           "Deal": {"id": deal["id"]}, "Phone": phone, "SMS_Text": text, "Sender": sender, "Template": INTRO_TEMPLATE}
    if _id(deal.get("Contact_Name")):
        row["Contact"] = {"id": _id(deal.get("Contact_Name"))}
    return _decision("send", "intro text", deal["id"], note="Intro text sent from %s's line: \"%s\"" % (sender, text),
                     outbox=row, sample_text=intro_text("", sender, tent, deal.get("Surgeon"), where))


def intro_sample(dec, to):
    """The owner's copy of a coordinator's intro text, from the same line:
    the same words without the patient's name, on no deal or contact."""
    sender = dec["outbox"]["Sender"]
    row = {"Name": "Intro sample - %s" % sender, "Phone": to, "SMS_Text": "[SAMPLE] " + dec["sample_text"],
           "Sender": sender, "Template": INTRO_SAMPLE_TEMPLATE}
    return _decision("send", "intro sample to the owner", None, outbox=row)


def summarize(decisions):
    """Counts only -- safe to print and to keep."""
    by = {}
    for d in decisions:
        k = d["result"] if d["result"] != "review" else "review: " + d["reason"]
        by[k] = by.get(k, 0) + 1
    return dict(sorted(by.items()))


class Store:
    """The ledger on disk. {seen: {key: {result, at, deal, reason}}, review:
    {key: {...}}, runs: [...], armed: bool}. A key is "pay:<payment id>",
    "merge:<note id>", "unsub:<deal id>" or "owner:<deal id>" -- the last two
    once per deal: a person who puts it back on Unsubscribe, or clears the
    coordinator, after this module acted is not overruled."""

    SETTLED = ("ok", "fixed", "flagged", "skip", "review", "sent")

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        # Armed out of the box, unlike the DNC ledger: this one only finishes
        # what the payment already started. PAID_GUARD_WRITE=0 or ?arm=0 stops it.
        self.data = {"seen": {}, "review": {}, "runs": [], "armed": True}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data.update({k: v for k, v in d.items() if k in ("seen", "review", "runs", "armed")})
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            self.data["load_error"] = "%s: %s" % (type(e).__name__, e)
        return self.data

    def save(self):
        tmp = str(self.path) + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=0, sort_keys=True)
            os.replace(tmp, str(self.path))

    def settled(self, key) -> bool:
        """A key whose write failed, or that was waiting out the grace period,
        is looked at again on the next run."""
        return (self.data["seen"].get(key) or {}).get("result") in self.SETTLED

    def record(self, key, decision, ok, now_iso):
        result = decision["result"]
        if result == "fix":
            result = "fixed" if ok else "error"
        elif result == "flag":
            result = "flagged" if ok else "error"
        elif result == "send":
            result = "sent" if ok else "error"
        with self._lock:
            self.data["seen"][key] = {"result": result, "at": now_iso, "deal": decision.get("deal"),
                                      "reason": decision.get("reason") or None}
            if result == "review":
                self.data["review"][key] = {"deal": decision.get("deal"), "reason": decision["reason"],
                                            "seen": now_iso}
            elif result in ("fixed", "flagged", "ok", "sent"):
                self.data["review"].pop(key, None)

    def note_run(self, info):
        with self._lock:
            self.data["runs"] = (self.data["runs"] + [info])[-40:]
