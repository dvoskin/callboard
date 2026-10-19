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

Every fifteen minutes this module checks the three, one record at a time:

  decide_payment(p, deal, handled, now)   a Books retainer payment
  decide_payment_merged(p, snapshot, survivor, handled, now)
                                          one whose deal was already merged away
  decide_merge(snapshot, survivor, handled)  a "Merged duplicate:" note
  decide_unsub(deal, handled, now)        a paid deal sitting on Unsubscribe

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

Names and numbers stay out of the logs.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta

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
# A paid deal on Unsubscribe is brought back only when the payment is this
# recent. An older one is a past patient who opted out of marketing.
RECENT_DAYS = 180

DEAL_FIELDS = ("id, Deal_Name, Stage, Retainer_Paid, Retainer_Paid_Date, Retainer_Invoice_URL, Payment_Method, "
               "Estimate_Sent, Contact_Name, Scheduling_Owner, Zoho_Books_Customer_ID, Created_Time, Modified_Time")
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
    "merge:<note id>" or "unsub:<deal id>" -- once per deal: a person who puts
    it back on Unsubscribe after this module moved it is not overruled."""

    SETTLED = ("ok", "fixed", "flagged", "skip", "review")

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
        with self._lock:
            self.data["seen"][key] = {"result": result, "at": now_iso, "deal": decision.get("deal"),
                                      "reason": decision.get("reason") or None}
            if result == "review":
                self.data["review"][key] = {"deal": decision.get("deal"), "reason": decision["reason"],
                                            "seen": now_iso}
            elif result in ("fixed", "flagged", "ok"):
                self.data["review"].pop(key, None)

    def note_run(self, info):
        with self._lock:
            self.data["runs"] = (self.data["runs"] + [info])[-40:]
