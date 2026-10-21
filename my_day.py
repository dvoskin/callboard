"""My Day: one rep's calls for today, built from Zoho.

Build plan step 21. Danny approved the mock-up on 2026-10-20 (artifact
6iFi9GhoBVn8i6CJeZP3Ch) with these answers:

  * My Day lives in the Call Tracker. Managers can open any rep's page.
  * "Late" is 15 minutes past the call time.
  * The rep sees the "talked, nothing booked" number.
  * Coordinators get their own buttons later (Confirmed, Rescheduled, Paid,
    Call back at a time, No answer). The role switch is here; sales ships first.

This module is the pure part, like callbacks_board.py: give it the raw Zoho rows
(as COQL returns them) and the time, and it returns the page. It reads nothing
from the network, so checks_my_day.py can drive every rule with fixtures.

The rules, in one place:

  Late       open calls (Scheduled or Overdue) whose time is more than
             LATE_MINUTES ago. Oldest first.
  Today      open calls whose time is today and not yet late, in time order,
             with a Now line between the past and the future.
  New leads  deals the rep owns, created in the last NEW_LEAD_HOURS, with no
             completed outbound call yet. Newest first.
  Queue      the same test on deals owned by the shared queue users, when the
             app is told who they are. The first rep to tap Call claims one.
  Done       completed calls today, grouped by Call Result. A completed call
             with no Call Result (blank or "Pending") is "no result yet" and
             raises the red bar.
  Talked, nothing booked
             completed today, at least TALK_MIN_SECONDS long, a Call Result
             that says a person was reached, and no open call on the same
             deal after it.

Added 2026-10-20 (Danny, same day):

  Start consultation
             every row with a deal opens the estimator for that deal in a new
             tab, the same URL as the "Start Consultation" button on a Zoho
             deal page.
  Pipeline   (sales only) four lists of the rep's OWN deals, each counted in
             its header and capped like the sections above:
               No next step   New Deal or Call Scheduled (NO_STEP_STAGES),
                              created in the last PIPE_DAYS, no open call on
                              the deal from now on. Oldest last contact
                              first. Quote Sent and Retainer Invoice Sent
                              deals have their own lists, so no deal is in
                              two of these lists.
               Quotes waiting Stage Quote Sent, Quote_Sent_Date in the last
                              PIPE_DAYS and not a 2026-09-24 bulk stamp
                              (BULK_STAMP_WINDOWS; same for invoices). Newest
                              quote first, days since it.
               Unpaid retainer invoices
                              Stage Retainer Invoice Sent, Retainer_Paid not
                              true, Retainer_Sent_Date in the last PIPE_DAYS
                              (no date: created in the last PIPE_DAYS).
                              Newest invoice first, days since it.
               Texted back    the back office's ReplyPing ledger: the rep's
                              patients whose latest text is unanswered. A
                              deal listed here is left out of the other
                              three; no deal is in two pipeline lists.
  Dialer tag a row from a RingCX dialer call ("Outbound Call to ...", or a
             dialer disposition on it) says "dialer".
  One conversation, one result
             a rep's call often leaves two Zoho records: the booked call and
             the dial RingCentral logged. They are paired (same patient, the
             booked call due no more than PAIR_EARLY_MINUTES after the dial)
             and counted as ONE conversation: done once, with a result when
             either record has one, and the booked half is no longer Late.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LATE_MINUTES = 15
TALK_MIN_SECONDS = 60
NEW_LEAD_HOURS = 24
LATE_SHOW = 25          # rows in Late before "+ N more"
TODAY_SHOW = 30
NEW_SHOW = 20
HISTORY_SHOW = 6

# The "Start Consultation" custom button on Zoho deal pages opens exactly this.
CONSULT_URL = "https://estimator.goalsplasticsurgery.com?dealid=%s"
# Danny, 2026-10-20 (second pass): 30 days, not 90. The 90-day lists ran to
# hundreds of deals per rep (Maria Carvajal: 609 no next step, 930 quotes).
PIPE_DAYS = 30
PIPE_SHOW = 20
PIPE_STAGES = ("New Deal", "Call Scheduled", "Quote Sent", "Retainer Invoice Sent")
# No next step only: a quoted or invoiced deal is in Quotes waiting or Unpaid
# retainer invoices instead, so the same patient is never listed twice.
NO_STEP_STAGES = ("New Deal", "Call Scheduled")
# The rep's completed calls are read this far back (last contact, history).
CALLS_DAYS = 30

# NOT A SEND DATE. On 2026-09-24 a bulk update stamped Quote_Sent_Date and
# Retainer_Sent_Date on thousands of deals at once (read-only count, all
# deals, 2026-10-20): Quote_Sent_Date 6,826 between 11:17 and 11:41 ET, 442 at
# 13:18-13:21 and ~50 at 23:00-23:04; Retainer_Sent_Date 1,036 at 13:18-13:21
# and 14 at 23:00-23:04. The rest of that day has one or two a minute, which
# is people. A deal stamped inside a batch has no real send date, so My Day
# leaves it out of Quotes waiting and Unpaid retainer invoices (Danny,
# 2026-10-20) rather than calling it "26 days since quote". Both fields are
# datetimes kept to the minute, so the test is the minute windows (Eastern),
# not the whole day: a quote sent by hand at 3 PM that day still counts.
BULK_STAMP_WINDOWS = {
    "Quote_Sent_Date": (("2026-09-24 11:17", "2026-09-24 11:41"),
                        ("2026-09-24 13:18", "2026-09-24 13:21"),
                        ("2026-09-24 23:00", "2026-09-24 23:04")),
    "Retainer_Sent_Date": (("2026-09-24 13:18", "2026-09-24 13:21"),
                           ("2026-09-24 23:00", "2026-09-24 23:04")),
}
_ET = ZoneInfo("America/New_York")


def is_bulk_stamp(field, value) -> bool:
    """True when this Quote_Sent_Date / Retainer_Sent_Date is one of the
    2026-09-24 bulk stamps (BULK_STAMP_WINDOWS), not a day anything was sent."""
    t = ts(value)
    if not t:
        return False
    minute = t.astimezone(_ET).strftime("%Y-%m-%d %H:%M")
    return any(a <= minute <= b for a, b in BULK_STAMP_WINDOWS.get(field, ()))
# Zoho Admin and Ariel Voskin hold unassigned leads (back office QUEUE_USER_IDS).
# Only used to keep "Missed Call" deals with no name off a queue user's own
# No next step list; MY_DAY_QUEUE_OWNER_IDS adds to it.
QUEUE_USER_IDS = ("5212466000000357001", "5212466000034327806")
# A booked call due this long after a dial is still that dial's conversation
# (the rep rang a few minutes early). Later than that it is a new attempt: the
# follow-up ladder books its next call after a no-answer, and that call is not
# the same conversation.
PAIR_EARLY_MINUTES = 10
ZOHO_DEAL_URL = "https://crm.zoho.com/crm/org771460636/tab/Potentials/%s"
ZOHO_CONTACT_URL = "https://crm.zoho.com/crm/org771460636/tab/Contacts/%s"

# Call_Result values. "Pending" is what RingCentral writes at creation, so it is
# not a result anybody chose.
NO_RESULT = {"", "-none-", "pending"}
# A person picked up. "Not interested" and "Lost" are reached too, but they are
# a finished conversation, not one that should have booked a next step, so they
# do not count toward "talked, nothing booked".
REACHED = {"reached", "interested", "requested more info", "requested call back",
           "consultation completed", "rescheduled call"}

# A dial the phone system logged ("Outgoing call to ...") rather than one that
# was scheduled. zoho_client._LOGGED_CALL also matches "Missed call", but here
# a "MISSED ..." subject is a scheduled try-again call and must stay a row.
LOGGED_CALL = re.compile(r"^\s*(outbound|outgoing|inbound|incoming)\s+call\b", re.I)
# The dials: RingEX logs "Outgoing call to ...", the RingCX dialer "Outbound Call to ...".
DIAL_CALL = re.compile(r"^\s*(outbound|outgoing)\s+call\b", re.I)
RINGCX_CALL = re.compile(r"^\s*outbound\s+call\b", re.I)
MISSED_DEAL = re.compile(r"^\s*missed call", re.I)
# Text Zoho or a workflow put in Description that is not a rep's note.
TEMPLATE_NOTE = re.compile(r"^\s*(before you call|auto-completed|call result was pending)", re.I)
# Stages where a fresh deal is not a lead to call.
DEAD_STAGE = re.compile(r"unsubscribe|lost|closed|do not|dnc|duplicate|junk", re.I)

OPEN = {"scheduled", "overdue"}

# ── the buttons ───────────────────────────────────────────────────────────
# key -> (label, hint, group, needs). group orders them under "Talked to her",
# "Stop calling" and "Did not reach her" as the mock does. needs: "time" opens
# the time picker, "reason" asks why, "confirm" asks once.
SALES_OUTCOMES = [
    ("booked_call",    "Booked a call",       "asks the time, texts her",   "talked", "time"),
    ("consult_booked", "Consult booked",      "asks the day",               "talked", "time"),
    ("quote_sent",     "Quote sent",          "we call back in 2 days",     "talked", ""),
    ("call_back",      "Call back at a time", "asks the time",              "talked", "time"),
    ("not_interested", "Not interested",      "no more calls",              "talked", "reason"),
    ("wrong_number",   "Wrong number",        "no more calls to it",        "stop",   ""),
    ("do_not_call",    "Do not call",         "stops calls and texts",      "stop",   "confirm"),
    ("no_answer",      "No answer",           "try again tomorrow",         "missed", ""),
    ("left_voicemail", "Left voicemail",      "try again tomorrow",         "missed", ""),
]
# Scaffolded for the coordinator variant. Not wired to the engine yet: the page
# shows them disabled with "Coming soon".
COORDINATOR_OUTCOMES = [
    ("confirmed",   "Confirmed",           "the patient confirmed",      "talked", ""),
    ("rescheduled", "Rescheduled",         "asks the new date",          "talked", "time"),
    ("paid",        "Paid",                "payment taken",              "talked", ""),
    ("call_back",   "Call back at a time", "asks the time",              "talked", "time"),
    ("no_answer",   "No answer",           "try again tomorrow",         "missed", ""),
]
OUTCOME_KEYS = {k for k, *_ in SALES_OUTCOMES}
REASONS = [("price", "Price"), ("not_ready", "Not ready"),
           ("went_elsewhere", "Went elsewhere"), ("other", "Other")]
REASON_KEYS = {k for k, _ in REASONS}

WHY_LABELS = {
    "new": "New lead", "quote": "Quote follow-up", "cb": "Asked for a call back",
    "booked": "Booked call", "retry": "No answer, try again",
    "nostep": "No next step", "quotewait": "Quote sent", "invoice": "Invoice unpaid",
    "texted": "Texted back", "task": "Follow-up due",
    # coordinator reasons (scaffold)
    "preop": "Pre-op call", "balance": "Balance due", "postop": "Post-op check",
    "missed": "Missed their call",
}


# Chance of closing by how far the deal got. Measured 2026-10-21 on the 21,663
# deals created 45 to 120 days earlier: retainer invoice sent, 49% paid; quote
# sent, 13%; every lead, 4.6%; never quoted, 2 of 14,264. Lists that mix stages
# put the patient closest to paying first.
CLOSE_RANK = {"Retainer Invoice Sent": 0, "Quote Sent": 1}


def close_rank(stage) -> int:
    return CLOSE_RANK.get((stage or "").strip(), 2)


def outcomes_for(role):
    rows = COORDINATOR_OUTCOMES if role == "coordinator" else SALES_OUTCOMES
    return [{"key": k, "label": l, "hint": h, "group": g, "needs": n} for k, l, h, g, n in rows]


# ── small helpers ─────────────────────────────────────────────────────────
def ts(value):
    """Zoho datetime ('2026-10-20T16:30:00-04:00') -> aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ref_id(v):
    return str(v.get("id") or "") if isinstance(v, dict) else ""


def ref_name(v):
    return (v.get("name") or "").strip() if isinstance(v, dict) else ""


def deal_person(deal) -> str:
    """The patient's name on a deal row. COQL returns a lookup as {"id"} only,
    so the name comes from Contact_Name.First_Name / .Last_Name, which
    zoho_client asks for; a fixture's {"name"} still works."""
    if not isinstance(deal, dict):
        return ""
    return ref_name(deal.get("Contact_Name")) or " ".join(
        x for x in (deal.get("Contact_Name.First_Name"), deal.get("Contact_Name.Last_Name")) if x).strip()


def digits10(value) -> str:
    d = "".join(ch for ch in str(value or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def fmt_phone(value) -> str:
    d = digits10(value)
    return "(%s) %s-%s" % (d[:3], d[3:6], d[6:]) if len(d) == 10 else ""


def short_name(full: str) -> str:
    """'Carmen Vega' -> 'Carmen V.' The list shows this; the opened row the full."""
    parts = (full or "").split()
    if not parts:
        return "Unknown"
    if len(parts) == 1:
        return parts[0]
    return "%s %s." % (parts[0], parts[-1][0].upper())


def first_name(full: str) -> str:
    parts = (full or "").split()
    return parts[0] if parts else ""


def result_of(call) -> str:
    """The rep's written result, or "" for none. Call_Result first; a dialer
    disposition counts too, because RingCX writes the agent's choice there."""
    r = (call.get("Call_Result") or "").strip()
    if r.lower() not in NO_RESULT:
        return r
    for f in ("Outgoing_call_disposition", "DialerDisposition"):
        v = (call.get(f) or "").strip()
        lv = v.lower()
        # "Disposition Timeout" and "[no-agent-disp...]" are the dialer giving
        # up waiting for the agent: exactly a call with no result written.
        if v and not lv.startswith("[no-agent-disp") and "timeout" not in lv and lv not in NO_RESULT:
            return v
    return ""


# Dialer dispositions that say nobody was talked to, or the talk is finished
# (same spirit as dnc_dispositions._NOT_REACHED, plus the closed outcomes).
_DISP_NOT_TALKED = ("no answer", "voicemail", "voice mail", "machine", "busy", "bad number", "wrong",
                    "disconnected", "dropped", "no contact", "not reached", "fax", "hang up", "hung up",
                    "invalid", "left message", "lvm", "timeout", "dnc", "do not call", "not interested",
                    "lost", "refund", "test", "n/a", "no disposition")


def is_reached(call) -> bool:
    """A person was talked to and the talk was not a closed "no". Call_Result
    first; when it is blank, the dialer's disposition."""
    r = (call.get("Call_Result") or "").strip().lower()
    if r and r not in NO_RESULT:
        return r in REACHED
    d = ((call.get("Outgoing_call_disposition") or call.get("DialerDisposition") or "")).strip().lower()
    return bool(d) and not d.startswith("[no-agent-disp") and not any(w in d for w in _DISP_NOT_TALKED)


def is_dialer(call) -> bool:
    """A RingCX dialer call: its subject, or a disposition the dialer wrote."""
    return bool(RINGCX_CALL.match(call.get("Subject") or "")
                or (call.get("Outgoing_call_disposition") or call.get("DialerDisposition") or "").strip())


def consult_url(deal_id) -> str:
    """The estimator for a Zoho deal, or "" when there is no real deal id."""
    d = str(deal_id or "").strip()
    return CONSULT_URL % d if d.isdigit() else ""


def days_since(value, now_utc):
    t = ts(value)
    if not t:
        return None
    return max(0, (now_utc - t).days)


def days_label(n) -> str:
    if n is None:
        return "date not set"
    if n == 0:
        return "today"
    return "1 day" if n == 1 else "%d days" % n


def pair_dials(calls, deal_contact, today, tz):
    """One conversation, one result: {dial call id: booked call} for the rep's
    dials today. A dial is a completed "Outgoing/Outbound call to" record; its
    booked half is a non-logged outbound call on the same patient (deal id, or
    the contact, through the deal's Contact_Name) that is open (any day) or was
    completed today, due no later than PAIR_EARLY_MINUTES after the dial. The
    nearest such call wins; each booked call pairs with one dial, earliest dial
    first."""
    def keys(c):
        k = set()
        did, cid = ref_id(c.get("What_Id")), ref_id(c.get("Who_Id"))
        if did:
            k.add("d" + did)
            if deal_contact.get(did):
                k.add("c" + deal_contact[did])
        if cid:
            k.add("c" + cid)
        return k

    dials, booked = [], []
    for c in calls:
        t = ts(c.get("Call_Start_Time"))
        if not t:
            continue
        subj = c.get("Subject") or ""
        st = status_of(c)
        if DIAL_CALL.match(subj):
            if counts_as_done(c) and t.astimezone(tz).date() == today:
                dials.append((t, c))
        elif (c.get("Call_Type") or "Outbound") == "Outbound" and not LOGGED_CALL.match(subj):
            if st in OPEN or (st == "completed" and t.astimezone(tz).date() == today):
                booked.append((t, c))
    dials.sort(key=lambda x: x[0])
    used, out = set(), {}
    for t, d in dials:
        dk = keys(d)
        best = None
        for bt, b in booked:
            bid = str(b.get("id"))
            if bid in used or not (dk & keys(b)) or bt > t + timedelta(minutes=PAIR_EARLY_MINUTES):
                continue
            if best is None or abs((bt - t).total_seconds()) < abs((best[0] - t).total_seconds()):
                best = (bt, b)
        if best:
            used.add(str(best[1].get("id")))
            out[str(d.get("id"))] = best[1]
    return out


def status_of(call) -> str:
    return (call.get("Outgoing_Call_Status") or "").strip().lower()


# Subjects of calls Zoho's ladders book on a rep's calendar. Zoho Admin books a
# "Quote Follow Up 1 - 24 Hours" for midnight the day after a quote, and at
# midnight Zoho marks it Completed with its default 5-minute duration. Nobody
# dialed: Charlotte's page showed five of them on 10-21 as "12:00 AM · no
# result" and as "Call, 5 min" in history, next to follow-ups she had booked
# herself. A real call is the dial record ("Outbound call to ..."), or one with
# a result written; this is neither.
ROBOT_BOOKED = re.compile(r"^\s*(missed - )?scheduled call\b|^\s*call scheduled\b|^\s*follow-up:"
                          r"|^\s*\d+(st|nd|rd|th) attempt\b|quote follow[ -]up|automated follow[ -]up", re.I)


def is_auto_completed(call) -> bool:
    """Nobody dialed: a workflow closed it ("Auto-completed by workflow"), or
    a ladder call Zoho completed by itself (robot subject, no result, no
    dialer disposition)."""
    if status_of(call) != "completed":
        return False
    if (call.get("Description") or "").strip().lower().startswith("auto-completed"):
        return True
    return bool(ROBOT_BOOKED.search(call.get("Subject") or "")) and not result_of(call)


def counts_as_done(call) -> bool:
    """A completed call a person made or took. A workflow's auto-completion is
    not a call, and neither is a Missed row."""
    if status_of(call) != "completed":
        return False
    if (call.get("Call_Type") or "") == "Missed":
        return False
    return not is_auto_completed(call)


def why(call, role="sales", deal_is_new=False) -> str:
    """The pill on a row. Order matters: a quote follow-up for a deal created
    an hour ago is still a quote follow-up."""
    s = (call.get("Subject") or "").lower()
    res = (call.get("Call_Result") or "").lower()
    if role == "coordinator":
        if "pre-op" in s or "pre op" in s or "labs" in s:
            return "preop"
        if "balance" in s or "payment" in s:
            return "balance"
        if "post-op" in s or "check-in" in s or "confirm surgery" in s:
            return "postop"
        if "call back" in s or "callback" in s or "call back" in res:
            return "cb"
        if (call.get("Call_Type") or "") == "Missed" or "missed" in s:
            return "missed"
        return "booked"
    if "quote follow up" in s or "quote follow-up" in s:
        return "quote"
    if "automated follow up" in s or "missed" in s or "no answer" in s:
        return "retry"
    if "call back" in s or "callback" in s or "best contact time" in s or "call back" in res:
        return "cb"
    if deal_is_new:
        return "new"
    return "booked"


def dur_label(seconds) -> str:
    try:
        s = int(float(seconds or 0))
    except (TypeError, ValueError):
        s = 0
    if s < 60:
        return "%d s" % s
    return "%d min" % round(s / 60)


def ago_label(delta: timedelta) -> str:
    m = int(delta.total_seconds() // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return "%d min ago" % m
    h, mm = divmod(m, 60)
    return "%d h %d m ago" % (h, mm) if mm else "%d h ago" % h


def late_label(delta: timedelta) -> str:
    m = int(delta.total_seconds() // 60)
    h, mm = divmod(m, 60)
    if h and mm:
        return "%d h %d m late" % (h, mm)
    if h:
        return "%d h late" % h
    return "%d m late" % mm


def clock(dt_local) -> str:
    return dt_local.strftime("%I:%M %p").lstrip("0")


def day_label(dt_local, today_local) -> str:
    d = dt_local.date()
    if d == today_local:
        return "Today"
    if d == today_local - timedelta(days=1):
        return "Yesterday"
    return dt_local.strftime("%b %-d")


# ── the page ──────────────────────────────────────────────────────────────
def build(world, now_utc, tz, role="sales", rep=None, queue_enabled=False, queue_ids=()):
    """world (raw Zoho rows):
         calls_open      the rep's Scheduled/Overdue calls from the late window to end of today
         calls_today     the rep's calls with a start time today (any status)
         deals_new       deals the rep owns created in the last NEW_LEAD_HOURS
         deals_queue     deals the queue users own created in the last NEW_LEAD_HOURS
         deals           {deal_id: deal row} for every deal on the page
         deal_calls      every call (any owner) on those deals since the history window
         contacts        {contact_id: contact row} for calls with no deal
       pipeline (sales; any may be missing, and the section then says so):
         pipe_deals      the rep's deals in PIPE_STAGES created, quoted or
                         invoiced in the last PIPE_DAYS
         future_open     open calls (any owner) from now on, for "next step"
         rep_calls       the rep's completed calls in the last 30 days
         replies         back office open reply pings, or None when it
                         could not be read
       now_utc aware datetime; tz a tzinfo for the clinic's clock.
    """
    rep = rep or {}
    now_local = now_utc.astimezone(tz)
    today = now_local.date()
    late_cut = now_utc - timedelta(minutes=LATE_MINUTES)

    deals = world.get("deals") or {}
    contacts = world.get("contacts") or {}
    pipe_deals = {str(d.get("id")): d for d in (world.get("pipe_deals") or []) if d.get("id")}

    # Deal <-> contact, from every deal row on the page. A dial RingCentral
    # logged on the contact only (a quarter of them on 2026-10-20) belongs to
    # the contact's deal when the page knows exactly one.
    deal_contact, contact_deals = {}, {}
    for did, d in list(pipe_deals.items()) + list(deals.items()):
        cid = ref_id(d.get("Contact_Name"))
        if cid:
            deal_contact[str(did)] = cid
            contact_deals.setdefault(cid, set()).add(str(did))

    def deal_of(c):
        did = ref_id(c.get("What_Id"))
        if did:
            return did
        ds = contact_deals.get(ref_id(c.get("Who_Id"))) or ()
        return next(iter(ds)) if len(ds) == 1 else ""

    # Every call on each deal, any owner, oldest first. History, last contact,
    # "has this lead been called" and "is there a next call" all read this.
    by_deal = {}
    seen = set()
    for c in list(world.get("deal_calls") or []) + list(world.get("calls_today") or []) \
            + list(world.get("calls_open") or []) + list(world.get("rep_calls") or []):
        cid = str(c.get("id") or "")
        if cid and cid in seen:
            continue
        seen.add(cid)
        did = deal_of(c)
        if did:
            by_deal.setdefault(did, []).append(c)
    for did in by_deal:
        by_deal[did].sort(key=lambda c: ts(c.get("Call_Start_Time")) or now_utc)

    def called_out(did):
        return any(counts_as_done(c) and (c.get("Call_Type") or "") == "Outbound"
                   for c in by_deal.get(did, ()))

    new_cut = now_utc - timedelta(hours=NEW_LEAD_HOURS)

    def deal_is_new(did):
        d = deals.get(did) or {}
        created = ts(d.get("Created_Time"))
        return bool(created and created >= new_cut and not called_out(did))

    def patient(c=None, deal=None):
        deal = deal if deal is not None else deals.get(deal_of(c or {})) or {}
        name = deal_person(deal) if deal else ""
        phone = digits10(deal.get("Phone")) if deal else ""
        cid = ref_id(deal.get("Contact_Name")) if deal else ""
        if c is not None:
            who = c.get("Who_Id")
            if not name:
                name = ref_name(who)
            if not cid:
                cid = ref_id(who)
        ct = contacts.get(cid) or {}
        if not name and ct:
            name = " ".join(x for x in (ct.get("First_Name"), ct.get("Last_Name")) if x)
        if not phone and ct:
            phone = digits10(ct.get("Mobile") or ct.get("Phone"))
        if not name and c is not None:
            name = ref_name(c.get("What_Id")) or (c.get("Subject") or "")
        return name.strip(), phone, cid

    def last_contact(did, before=None):
        """The most recent completed call on the deal before `before`."""
        for c in reversed(by_deal.get(did, [])):
            t = ts(c.get("Call_Start_Time"))
            if not counts_as_done(c) or not t or (before and t >= before):
                continue
            res = result_of(c)
            what = res or ("talked " + dur_label(c.get("Call_Duration_in_seconds"))
                           if int(float(c.get("Call_Duration_in_seconds") or 0)) >= TALK_MIN_SECONDS
                           else "no result written")
            return {"date": day_label(t.astimezone(tz), today), "what": what.lower() if res else what}
        return None

    def note_for(did, c=None):
        """The last real note: a completed call's Description that a person
        wrote (not the script Zoho puts on every scheduled call)."""
        cands = list(by_deal.get(did, [])) if did else ([c] if c else [])
        for x in reversed(cands):
            text = (x.get("Description") or "").strip()
            if not text or TEMPLATE_NOTE.match(text) or status_of(x) != "completed":
                continue
            t = ts(x.get("Call_Start_Time"))
            return {"text": text[:600], "by": ref_name(x.get("Owner")),
                    "date": day_label(t.astimezone(tz), today) if t else ""}
        return None

    def history(did, skip=""):
        """The patient's calls, newest first. Calls still ahead are marked
        "up" (the page shows them under "Coming up", not "Before this"); the
        row's own call is left out, it is the row."""
        out = []
        for c in reversed(by_deal.get(did, [])):
            t = ts(c.get("Call_Start_Time"))
            st = status_of(c)
            if not t or st == "cancelled" or is_auto_completed(c) or (skip and str(c.get("id")) == skip):
                continue
            if t > now_utc and st in OPEN:
                out.append({"d": day_label(t.astimezone(tz), today), "k": "Next call",
                            "x": clock(t.astimezone(tz)), "by": ref_name(c.get("Owner")), "up": True})
                continue
            elif st in OPEN:
                continue            # a past open call is a row on this page, not history
            else:
                kind = "Call, " + dur_label(c.get("Call_Duration_in_seconds"))
                x = result_of(c) or "no result written"
            out.append({"d": day_label(t.astimezone(tz), today), "k": kind, "x": x,
                        "by": ref_name(c.get("Owner"))})
            if sum(1 for h in out if not h.get("up")) >= HISTORY_SHOW:
                break
        d = deals.get(did) or {}
        created = ts(d.get("Created_Time"))
        if created and sum(1 for h in out if not h.get("up")) < HISTORY_SHOW:
            out.append({"d": day_label(created.astimezone(tz), today), "k": "New lead",
                        "x": d.get("Lead_Source") or "", "by": ""})
        return out

    def facts(deal, phone, cid):
        f = []
        if phone:
            f.append(("Phone", fmt_phone(phone)))
        if deal.get("Procedure"):
            f.append(("Wants", deal.get("Procedure")))
        if deal.get("Amount"):
            try:
                f.append(("Quote", "${:,.0f}".format(float(deal.get("Amount")))))
            except (TypeError, ValueError):
                pass
        if deal.get("Lead_Source"):
            f.append(("Came from", deal.get("Lead_Source")))
        if deal.get("Language"):
            f.append(("Speaks", deal.get("Language")))
        if deal.get("Location"):
            f.append(("Location", str(deal.get("Location")).title()))
        if deal.get("Stage"):
            f.append(("Stage", deal.get("Stage")))
        if deal.get("Timezone"):
            f.append(("Her zone", deal.get("Timezone")))
        return [{"k": k, "v": str(v)} for k, v in f]

    def meta_line(deal, phone):
        bits = [deal.get("Procedure") or "", str(deal.get("Location") or "").title()]
        lang = (deal.get("Language") or "").strip()
        bits.append(lang if lang and lang.lower() != "english" else fmt_phone(phone))
        return " · ".join(b for b in bits if b)

    def row(c=None, deal=None, kind=""):
        if deal is None:
            did = deal_of(c or {})
            deal = deals.get(did) or pipe_deals.get(did) or {}
        else:
            did = str(deal.get("id") or "")
        name, phone, cid = patient(c, deal)
        start = ts(c.get("Call_Start_Time")) if c else ts(deal.get("Created_Time"))
        start_local = start.astimezone(tz) if start else None
        r = {
            "key": (str(c.get("id")) if c else "d" + did),
            "call_id": str(c.get("id") or "") if c else "",
            "deal_id": did, "contact_id": cid,
            "start": start.isoformat() if start else "",
            "when": "", "sub": "", "sub_kind": "",
            "short": short_name(name), "full": name or "Unknown",
            "first": first_name(name),
            "meta": meta_line(deal, phone),
            "phone": phone, "phone_label": fmt_phone(phone),
            "subject": (c.get("Subject") or "") if c else "",
            "result": result_of(c) if c else "",
            "zoho": (ZOHO_DEAL_URL % did) if did else ((ZOHO_CONTACT_URL % cid) if cid else ""),
            "consult": consult_url(did),
            "dialer": bool(c is not None and is_dialer(c)),
        }
        if c is not None:
            r["why"] = why(c, role, deal_is_new(did) if did else False)
        else:
            r["why"] = "new"
        r["why_label"] = WHY_LABELS.get(r["why"], "")
        # Last contact is the latest one before now. A no-result row IS a
        # contact, so it looks before itself.
        lc = last_contact(did, before=start if kind == "noresult" else None) if did else None
        r["last"] = lc or {"date": "", "what": "never called" if r["why"] == "new" else "no calls on record"}
        r["note"] = note_for(did, c)
        r["history"] = history(did, skip=r["call_id"]) if did else []
        r["facts"] = facts(deal, phone, cid) if deal else ([{"k": "Phone", "v": fmt_phone(phone)}] if phone else [])
        if start_local:
            if kind == "late":
                r["when"] = clock(start_local) if start_local.date() == today else \
                    start_local.strftime("%b %-d, ") + clock(start_local)
                r["sub"], r["sub_kind"] = late_label(now_utc - start), "late"
            elif kind == "today":
                r["when"] = clock(start_local)
                mins = int((start - now_utc).total_seconds() // 60)
                if 0 <= mins <= 60:
                    r["sub"], r["sub_kind"] = "in %d min" % mins, "soon"
                elif mins < 0:
                    r["sub"], r["sub_kind"] = "now", "soon"
            elif kind in ("new", "queue"):
                r["when"] = ago_label(now_utc - start)
                src = deal.get("Lead_Source") or ""
                r["last"] = {"date": "", "what": (src + " · " if src else "") + "never called"}
            elif kind == "noresult":
                r["when"] = clock(start_local)
                r["sub"], r["sub_kind"] = "no result", "late"
        return r

    # ---- one conversation, one result: pair each dial with its booked call
    own = {}
    for c in list(world.get("calls_today") or []) + list(world.get("calls_open") or []):
        own.setdefault(str(c.get("id") or ""), c)
    pairs = pair_dials(own.values(), deal_contact, today, tz)
    paired_booked = {str(b.get("id")) for b in pairs.values()}

    # ---- open calls: late and today
    open_calls = {}
    for c in list(world.get("calls_open") or []) + list(world.get("calls_today") or []):
        if status_of(c) not in OPEN or (c.get("Call_Type") or "Outbound") != "Outbound":
            continue
        if LOGGED_CALL.match(c.get("Subject") or ""):
            continue
        # Already rung today: the dial is this call's conversation. It is
        # done (with or without a result, which the red bar asks for), not late.
        if str(c.get("id")) in paired_booked:
            continue
        open_calls[str(c.get("id"))] = c
    late, upcoming = [], []
    end_of_today = datetime.combine(today + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc)
    for c in open_calls.values():
        t = ts(c.get("Call_Start_Time"))
        if not t:
            continue
        if t < late_cut:
            late.append((t, c))
        elif t < end_of_today:
            upcoming.append((t, c))
    # Late: closest to paying first (invoice, then quote), oldest first within.
    # Today stays in time order: it is a schedule of promised times.
    late.sort(key=lambda x: (close_rank((deals.get(deal_of(x[1])) or {}).get("Stage")), x[0]))
    upcoming.sort(key=lambda x: x[0])

    def dedupe(pairs):
        """One row per patient per section: the ladder books the same deal
        more than once, and two rows for one person reads as two people."""
        out, seen_d = [], set()
        for t, c in pairs:
            k = ref_id(c.get("What_Id")) or ref_id(c.get("Who_Id")) or str(c.get("id"))
            if k in seen_d:
                continue
            seen_d.add(k)
            out.append((t, c))
        return out

    late = dedupe(late)
    upcoming = dedupe(upcoming)
    late_rows = [row(c, kind="late") for _, c in late]
    today_rows = [row(c, kind="today") for _, c in upcoming]
    now_index = sum(1 for t, _ in upcoming if t <= now_utc)

    # ---- done today, one entry per conversation
    # A booked call paired with a dial is that dial's other record: it is not
    # a second call done, and its result is the conversation's result too.
    def conv_result(c):
        b = pairs.get(str(c.get("id")))
        return result_of(c) or (result_of(b) if b is not None else "")

    done = []
    for c in world.get("calls_today") or []:
        t = ts(c.get("Call_Start_Time"))
        if not t or t.astimezone(tz).date() != today or not counts_as_done(c):
            continue
        if str(c.get("id")) in paired_booked:
            continue
        done.append((t, c))
    done.sort(key=lambda x: x[0])
    no_result = [(t, c) for t, c in done if not conv_result(c)]
    summary = {}
    for _, c in done:
        k = conv_result(c) or "No result yet"
        summary[k] = summary.get(k, 0) + 1
    done_summary = sorted(({"label": k, "n": n} for k, n in summary.items()),
                          key=lambda x: (x["label"] == "No result yet", -x["n"], x["label"]))

    # ---- talked, nothing booked
    def has_next(c, t):
        did = deal_of(c)
        pool = by_deal.get(did, []) if did else []
        return any(status_of(x) in OPEN and (ts(x.get("Call_Start_Time")) or t) > t
                   and str(x.get("id")) not in paired_booked for x in pool)

    def conv_reached(c):
        """Reached by whichever record carries the result (the dial first)."""
        b = pairs.get(str(c.get("id")))
        if result_of(c) or b is None or not result_of(b):
            return is_reached(c)
        return is_reached(b)

    talked_none = []
    for t, c in done:
        try:
            secs = int(float(c.get("Call_Duration_in_seconds") or 0))
        except (TypeError, ValueError):
            secs = 0
        if secs >= TALK_MIN_SECONDS and conv_reached(c) and not has_next(c, t):
            talked_none.append(c)

    # ---- new leads (and the shared queue)
    shown = {r["deal_id"] for r in late_rows + today_rows if r["deal_id"]}

    def fresh(rows_in):
        out = []
        for d in rows_in or []:
            did = str(d.get("id") or "")
            created = ts(d.get("Created_Time"))
            if not did or not created or created < new_cut or created > now_utc + timedelta(minutes=5):
                continue
            if DEAD_STAGE.search(d.get("Stage") or ""):
                continue
            if called_out(did):
                continue
            out.append((created, d))
        out.sort(key=lambda x: x[0], reverse=True)
        return out

    new_all = fresh(world.get("deals_new"))
    new_rows = [row(deal=d, kind="new") for _, d in new_all if str(d.get("id")) not in shown]
    queue_rows = []
    if queue_enabled:
        for _, d in fresh(world.get("deals_queue")):
            r = row(deal=d, kind="queue")
            r["claim"] = True
            queue_rows.append(r)

    def cap(rows_in, n):
        return rows_in[:n], max(0, len(rows_in) - n)

    pipeline = build_pipeline(world, now_utc, tz, rep, pipe_deals, by_deal, deal_of, row, cap,
                              queue_ids) if role == "sales" else {"enabled": False}

    late_show, late_more = cap(late_rows, LATE_SHOW)
    today_show, today_more = cap(today_rows, TODAY_SHOW)
    new_show, new_more = cap(new_rows, NEW_SHOW)
    queue_show, queue_more = cap(queue_rows, NEW_SHOW)

    page = {
        "rep": {"id": str(rep.get("id") or ""), "name": rep.get("name") or "",
                "short": short_name(rep.get("name") or "")},
        "role": role,
        "now": now_utc.isoformat(),
        "now_label": clock(now_local),
        "date_label": now_local.strftime("%A, %b %-d"),
        "late_minutes": LATE_MINUTES,
        "nums": {
            "done": len(done),
            "results": len(done) - len(no_result),
            "late": len(late_rows),
            "talked_nothing_booked": len(talked_none),
            "new_leads": len(new_all),
        },
        "no_result": [row(c, kind="noresult") for _, c in no_result],
        "late": late_show, "late_more": late_more,
        "late_oldest": late_rows[0]["when"] if late_rows else "",
        "today": today_show, "today_more": today_more, "now_index": min(now_index, len(today_show)),
        "today_last": today_rows[-1]["when"] if today_rows else "",
        "new_leads": new_show, "new_more": new_more,
        "new_elsewhere": len(new_all) - len(new_rows),
        "queue": queue_show, "queue_more": queue_more, "queue_enabled": bool(queue_enabled),
        "done_summary": done_summary,
        "outcomes": outcomes_for(role),
        "reasons": [{"key": k, "label": l} for k, l in REASONS],
        "outcomes_live": role != "coordinator",
        "pipeline": pipeline,
    }
    page["up_next"] = up_next(page, now_utc)
    return page


UP_NEXT_SHOW = 3
TASK_DAYS = 3              # a Zoho task due up to this many days ago is still a follow-up to do
DUE_SOON_MINUTES = 5       # a booked call this close to its time is due now
FRESH_LEAD_MINUTES = 15    # a new lead this young beats everything but a promised time


def up_next(page, now_utc, show=UP_NEXT_SHOW):
    """Priority and time in one list: what to do right now (Danny, 10-21: "in
    priority order of what has the highest chance of closing ... it should also
    somehow be chronological").

    A time we promised comes first, because the patient is waiting for it: late
    booked calls, then calls due within DUE_SOON_MINUTES. Then a new lead under
    FRESH_LEAD_MINUTES old, then a patient who texted back. After that, the
    flexible work by chance of closing: unpaid invoices (about 1 in 2 pay),
    quotes (1 in 8), older new leads, no next step. One row per patient. Also
    returns the next booked call after the due ones, so the rep knows the gap."""
    def mins_to(r):
        t = ts(r.get("start"))
        return (t - now_utc).total_seconds() / 60 if t else None
    pl = page.get("pipeline") or {}
    lists = lambda k: ((pl.get(k) or {}).get("rows") or [])
    today = page.get("today") or []
    due = [r for r in today if (mins_to(r) or 0) <= DUE_SOON_MINUTES]
    fresh = [r for r in page.get("new_leads") or [] if (mins_to(r) or -1e9) >= -FRESH_LEAD_MINUTES]
    steps = [
        (page.get("late") or [], lambda r: r.get("sub") or "Late"),
        (due, lambda r: "Due now"),
        (fresh, lambda r: "New lead, call now"),
        (lists("texts"), lambda r: "Texted you"),
        (lists("tasks"), lambda r: "Follow-up " + r.get("when", "due")),
        (lists("invoices"), lambda r: "Invoice unpaid, 1 in 2 pay"),
        (lists("quotes"), lambda r: "Quote sent, 1 in 8 pay"),
        (page.get("new_leads") or [], lambda r: "New lead"),
        (lists("no_step"), lambda r: "No next step"),
    ]
    out, seen = [], set()
    for rows, why_now in steps:
        for r in rows:
            k = r.get("deal_id") or r.get("phone") or r.get("key")
            if k in seen:
                continue
            seen.add(k)
            out.append(dict(r, key="up" + str(r.get("key")), sub=why_now(r), sub_kind="up"))
            if len(out) >= show:
                break
        if len(out) >= show:
            break
    nxt = next((r for r in today if (mins_to(r) or 0) > DUE_SOON_MINUTES), None)
    return {"rows": out, "next_booked": {"when": nxt["when"], "short": nxt["short"]} if nxt else None}


def build_pipeline(world, now_utc, tz, rep, pipe_deals, by_deal, deal_of, row, cap, queue_ids=()):
    """The rep's own pipeline: No next step, Quotes waiting, Unpaid retainer
    invoices, Texted back. Each list is {"rows", "more", "n"}; a list whose
    source could not be read carries "error" and no rows, never a false zero."""
    rep_id = str((rep or {}).get("id") or "")
    pipe_cut = now_utc - timedelta(days=PIPE_DAYS)
    queue = {str(q) for q in tuple(QUEUE_USER_IDS) + tuple(queue_ids or ())}
    have_pipe = world.get("pipe_deals") is not None
    have_future = world.get("future_open") is not None

    # Deals with an open call from now on, any owner: they have a next step.
    booked_ahead = {}
    for c in world.get("future_open") or []:
        t = ts(c.get("Call_Start_Time"))
        did = deal_of(c)
        if did and t and t >= now_utc and status_of(c) in OPEN:
            if did not in booked_ahead or t < booked_ahead[did]:
                booked_ahead[did] = t

    def last_done(did):
        for c in reversed(by_deal.get(did, [])):
            t = ts(c.get("Call_Start_Time"))
            if t and t <= now_utc and counts_as_done(c):
                return t
        return None

    def mine(d):
        return not rep_id or ref_id(d.get("Owner")) in ("", rep_id)

    def next_label(did):
        t = booked_ahead.get(did)
        if not t:
            return "no next call"
        loc = t.astimezone(tz)
        today_l = now_utc.astimezone(tz).date()
        return "next call " + ("today" if loc.date() == today_l else day_label(loc, today_l)) + " " + clock(loc)

    def with_next(r):
        """The last-contact column also says whether a call is booked ahead."""
        lc = r.get("last") or {}
        r["last"] = dict(lc, what=" · ".join(x for x in (lc.get("what"), next_label(r["deal_id"])) if x))
        r["next"] = next_label(r["deal_id"])

    def finish(rows_in, total_rows, error=""):
        show, more = cap(rows_in, PIPE_SHOW)
        out = {"rows": show, "more": more, "n": len(total_rows)}
        if error:
            out["error"] = error
        return out

    def prow(d, prefix, why):
        r = row(deal=d, kind="pipe")
        did = r["deal_id"]
        r["key"] = prefix + did
        r["why"], r["why_label"] = why, WHY_LABELS.get(why, "")
        return r

    # A patient who texted back is listed there, first, and in no other list:
    # one deal, one row in the pipeline (Danny, 2026-10-20).
    texted = {str(p.get("dealId") or "") for p in (world.get("replies") or [])} - {""}

    # ---- Follow-ups due: the rep's open Zoho tasks due today or in the last
    # TASK_DAYS days. They have a day, not a time: work them between booked
    # calls. Older ones are the robot backlog (184,320 open on 10-21, 179,925 of
    # them over a month old) and are left out. One row per patient, the latest
    # step; a patient here is in no list below it (texted back stays first).
    have_tasks = world.get("tasks") is not None
    today_local = now_utc.astimezone(tz).date()
    task_cut = today_local - timedelta(days=TASK_DAYS)
    tasks = {}
    for t in world.get("tasks") or []:
        did = ref_id(t.get("What_Id"))
        try:
            due = datetime.strptime(str(t.get("Due_Date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if not did or did in texted or due > today_local or due < task_cut:
            continue
        if did not in tasks or due > tasks[did][0]:
            tasks[did] = (due, t)
    tk = []
    for did, (due, t) in tasks.items():
        d = pipe_deals.get(did) or (world.get("deals") or {}).get(did) or (world.get("task_deals") or {}).get(did) or {
            "id": did, "Deal_Name": (t.get("What_Id") or {}).get("name") or "",
            "Contact_Name": {"id": ref_id(t.get("Who_Id")), "name": (t.get("Who_Id") or {}).get("name") or ""}}
        if (d.get("Stage") or "").startswith("Closed") or (d.get("Stage") or "") in ("Unsubscribe", "Expired", "Duplicates / Junk"):
            continue
        tk.append((close_rank(d.get("Stage")), -due.toordinal(), d, t, due))
    tk.sort(key=lambda x: (x[0], x[1]))
    k_rows = []
    for _, _, d, t, due in tk:
        r = prow(d, "tk", "task")
        n = (today_local - due).days
        r["when"] = "due today" if n == 0 else ("due yesterday" if n == 1 else "due %d days ago" % n)
        r["sub"], r["sub_kind"] = (t.get("Subject") or "")[:60], "q"
        r["task_id"] = str(t.get("id") or "")
        with_next(r)
        k_rows.append(r)
    texted = texted | set(tasks)

    # ---- No next step
    nostep = []
    for did, d in pipe_deals.items():
        if did in texted:
            continue
        if not mine(d) or (d.get("Stage") or "") not in NO_STEP_STAGES:
            continue
        created = ts(d.get("Created_Time"))
        if not created or created < pipe_cut:
            continue
        if did in booked_ahead:
            continue
        # A "Missed Call" deal with no name is the shared queue's to sort out,
        # not a pipeline deal; on a rep's own page it stays (the rep owns it).
        if rep_id in queue and MISSED_DEAL.match(d.get("Deal_Name") or "") \
                and deal_person(d).lower() in ("", "unknown"):
            continue
        nostep.append((last_done(did), created, d))
    # Oldest last contact first; never contacted is oldest of all, then by age.
    far = datetime.min.replace(tzinfo=timezone.utc)
    nostep.sort(key=lambda x: (x[0] or far, x[1]))
    ns_rows = []
    for lc, created, d in nostep:
        r = prow(d, "ns", "nostep")
        # Calls are read CALLS_DAYS back: a deal older than that with none in
        # the window was called before it, or never; the page cannot say which.
        r["when"] = ago_days(lc, now_utc) if lc else (
            "never called" if created > now_utc - timedelta(days=CALLS_DAYS) else "%d+ days ago" % CALLS_DAYS)
        r["sub"], r["sub_kind"] = "no next call", "late"
        ns_rows.append(r)

    # ---- Quotes waiting
    quotes = []
    for did, d in pipe_deals.items():
        if not mine(d) or (d.get("Stage") or "") != "Quote Sent" or did in texted:
            continue
        qt = ts(d.get("Quote_Sent_Date"))
        if not qt or qt < pipe_cut or is_bulk_stamp("Quote_Sent_Date", d.get("Quote_Sent_Date")):
            continue
        quotes.append((qt, d))
    quotes.sort(key=lambda x: x[0], reverse=True)
    q_rows = []
    for qt, d in quotes:
        r = prow(d, "qt", "quotewait")
        r["when"] = days_label(days_since(d.get("Quote_Sent_Date"), now_utc))
        r["sub"], r["sub_kind"] = "since quote", "q"
        with_next(r)
        q_rows.append(r)

    # ---- Unpaid retainer invoices
    invs = []
    for did, d in pipe_deals.items():
        if not mine(d) or (d.get("Stage") or "") != "Retainer Invoice Sent" or d.get("Retainer_Paid") is True \
                or did in texted:
            continue
        rt = ts(d.get("Retainer_Sent_Date"))
        if rt and rt < pipe_cut:
            continue
        # A bulk stamp is not a send date, and not "no date" either: the deal
        # is left out, not counted by its created date.
        if is_bulk_stamp("Retainer_Sent_Date", d.get("Retainer_Sent_Date")):
            continue
        if not rt:
            created = ts(d.get("Created_Time"))
            if not created or created < pipe_cut:
                continue
        invs.append((rt, d))
    # Newest invoice first; an invoice with no date goes last.
    invs.sort(key=lambda x: x[0] or far, reverse=True)
    i_rows = []
    for rt, d in invs:
        r = prow(d, "iv", "invoice")
        r["when"] = days_label(days_since(d.get("Retainer_Sent_Date"), now_utc))
        r["sub"], r["sub_kind"] = ("since invoice", "q") if rt else ("", "")
        with_next(r)
        i_rows.append(r)

    # ---- Texted back (the back office's ReplyPing ledger)
    replies = world.get("replies")
    t_rows = []
    for p in replies or []:
        did = str(p.get("dealId") or "")
        d = pipe_deals.get(did) or (world.get("deals") or {}).get(did) or {
            "id": did, "Contact_Name": {"name": p.get("patientName") or "", "id": ""}, "Phone": p.get("phone") or ""}
        if not d.get("Phone") and p.get("phone"):
            d = dict(d, Phone=p.get("phone"))
        r = prow(d, "tx", "texted")
        t = ts(p.get("inboundAt"))
        r["when"] = ago_label(now_utc - t) if t else ""
        r["start"] = t.isoformat() if t else ""
        text = (p.get("lastText") or "").strip()
        r["last"] = {"date": "", "what": ("“%s”" % text) if text else "texted, words not kept"}
        r["sub"], r["sub_kind"] = ("%d texts" % p["textCount"], "q") if int(p.get("textCount") or 1) > 1 else ("", "")
        r["key"] = "tx" + (did or digits10(p.get("phone")))
        t_rows.append((close_rank(d.get("Stage")), r))
    # Invoice, then quote, then the rest; the back office's order (newest) within.
    t_rows = [r for _, r in sorted(t_rows, key=lambda x: x[0])]

    nope = "Zoho could not be read for this list."
    return {
        "enabled": True,
        "days": PIPE_DAYS,
        "no_step": finish(ns_rows, nostep, "" if (have_pipe and have_future) else nope),
        "quotes": finish(q_rows, quotes, "" if have_pipe else nope),
        "invoices": finish(i_rows, invs, "" if have_pipe else nope),
        "texts": finish(t_rows, t_rows,
                        "" if replies is not None else "Couldn't load the texts just now."),
        "tasks": finish(k_rows, tk, "" if have_tasks else "Zoho tasks could not be read just now."),
    }


def ago_days(t, now_utc) -> str:
    """'today', '1 day ago', '12 days ago'."""
    n = max(0, (now_utc - t).days)
    if n == 0:
        return "today"
    return "1 day ago" if n == 1 else "%d days ago" % n


# ── the time picker ───────────────────────────────────────────────────────
def free_slots(busy_rows, now_utc, tz, days=3, start_hour=9, end_hour=19, step=30, limit=60):
    """Open slots on the rep's calendar: every `step`-minute start in working
    hours over the next `days` days that is at least 15 minutes away and does
    not overlap a booked call or a meeting.

    busy_rows: Zoho Calls (Call_Start_Time, Call_Duration_in_seconds) and
    Events (Start_DateTime, End_DateTime). The automated follow-up ladder is
    left out by the caller: Zoho books those on the hour by the hundred, and a
    rep with a 3 PM ladder call is not busy at 3 PM.
    """
    spans = []
    for r in busy_rows or []:
        a = ts(r.get("Call_Start_Time") or r.get("Start_DateTime"))
        if not a:
            continue
        b = ts(r.get("End_DateTime"))
        if not b:
            try:
                secs = int(float(r.get("Call_Duration_in_seconds") or 0))
            except (TypeError, ValueError):
                secs = 0
            b = a + timedelta(seconds=max(secs, 900))
        spans.append((a, b))
    out = []
    earliest = now_utc + timedelta(minutes=15)
    day0 = now_utc.astimezone(tz).date()
    for i in range(days + 3):
        d = day0 + timedelta(days=i)
        if d.weekday() == 6:          # Sunday
            continue
        t = datetime.combine(d, datetime.min.time(), tz).replace(hour=start_hour)
        stop = t.replace(hour=end_hour)
        while t < stop:
            tu = t.astimezone(timezone.utc)
            te = tu + timedelta(minutes=step)
            if tu >= earliest and not any(a < te and b > tu for a, b in spans):
                out.append({"at": tu.isoformat(), "day": day_label(t, day0) if t.date() != day0 else "Today",
                            "label": clock(t), "long": long_when(tu, tz)})
            t += timedelta(minutes=step)
        if len({s["day"] for s in out}) >= days or len(out) >= limit * 3:
            break
    # Thin to the first `limit` per day so the picker stays one screen.
    per_day, thin = {}, []
    for s in out:
        if per_day.get(s["day"], 0) < limit // max(days, 1):
            per_day[s["day"]] = per_day.get(s["day"], 0) + 1
            thin.append(s)
    return thin


def long_when(at_utc, tz) -> str:
    when = at_utc.astimezone(tz)
    return "%s at %s" % (when.strftime("%A, %B %-d"), clock(when))


def confirmation_text(patient_first, rep_first, when_text):
    """What the "Booked a call" text would say. The back office sends the real
    one; this is the preview beside the Send toggle. Patient text voice: no
    hyphens or dashes, one emoji, warm, no pressure."""
    return ("Hi %s, this is %s from Goals Plastic Surgery 😊 Thanks for talking with me. "
            "I will call you on %s. If another time works better, just reply here."
            % (patient_first or "there", rep_first or "your coordinator", when_text))


def confirmation_preview(patient_first, rep_first, at_utc, tz):
    return confirmation_text(patient_first, rep_first, long_when(at_utc, tz))


def is_shared_queue_owner(owner_id, queue_ids) -> bool:
    return str(owner_id or "") in {str(q) for q in (queue_ids or ())}


# ── fake data (local screenshots and checks; never on in production) ──────
def fake_world(now_utc, tz, rep_id="u-jess"):
    """A day shaped like the mock: four late, six today, five new leads,
    23 done with two missing a result. Made-up people, 555 numbers."""
    now_local = now_utc.astimezone(tz)
    today = now_local.date()

    def at(h, m=0, day=0):
        return datetime.combine(today + timedelta(days=day), datetime.min.time(), tz) \
            .replace(hour=h, minute=m).isoformat()

    def rel(minutes):
        return (now_utc + timedelta(minutes=minutes)).astimezone(tz).isoformat()

    people = [
        ("d1", "Shante Wilson", "5550113378", "BBL", "PENNSYLVANIA", "English", "Instagram", None, 3),
        ("d2", "Carmen Vega", "5550190042", "Tummy tuck", "New York", "English", "Instagram", 11200, 6),
        ("d3", "Lauren Pierce", "5550128861", "Breast lift", "NEW JERSEY", "English", "Website", None, 2),
        ("d4", "Daniela Cruz", "5550150100", "Lipo 360", "New York", "Spanish", "Facebook", None, 0.03),
        ("d5", "Tanya Brooks", "5550174410", "BBL", "New York", "English", "Facebook", None, 4),
        ("d6", "Nicole Hart", "5550162207", "Arm lift", "MARYLAND", "English", "Website", None, 5),
        ("d7", "Brittany Cole", "5550185530", "Lipo 360", "NEW JERSEY", "English", "Instagram", 7900, 3),
        ("d8", "Yesenia Ortiz", "5550120055", "BBL", "New York", "Spanish", "Facebook", None, 2),
        ("d9", "Ana Reyes", "5550133301", "Tummy tuck", "New York", "Spanish", "Instagram", None, 0.004),
        ("d10", "Monique Davis", "5550133302", "BBL", "NEW JERSEY", "English", "Website", None, 0.015),
        ("d11", "Keisha Grant", "5550133303", "Lipo 360", "New York", "English", "Facebook", None, 0.04),
        ("d12", "Rosa Medina", "5550133304", "Breast lift", "New York", "Spanish", "Instagram", None, 0.06),
        ("d13", "Jade Turner", "5550133305", "BBL", "MARYLAND", "English", "Website", None, 0.09),
        ("d14", "Erica Lane", "5550133306", "Tummy tuck", "New York", "English", "Facebook", None, 0.1),
        ("d15", "Paola Ruiz", "5550133307", "Lipo 360", "New York", "Spanish", "Instagram", None, 0.2),
    ]
    deals = {}
    for did, name, phone, proc, loc, lang, src, amt, age_days in people:
        deals[did] = {"id": did, "Deal_Name": name, "Contact_Name": {"name": name, "id": "c" + did},
                      "Phone": phone, "Stage": "Quote Sent" if amt else "New Lead", "Amount": amt,
                      "Lead_Source": src, "Language": lang, "Location": loc, "Procedure": proc,
                      "Created_Time": rel(-int(age_days * 1440)), "Timezone": "ET"}
    owner = {"id": rep_id, "name": "M."}

    def call(cid, did, subject, start, status="Scheduled", result=None, secs=None, desc=None,
             ctype="Outbound", who_owner=None):
        return {"id": cid, "What_Id": {"id": did, "name": deals[did]["Deal_Name"]},
                "Who_Id": {"id": "c" + did, "name": deals[did]["Contact_Name"]["name"]},
                "Subject": subject, "Call_Start_Time": start, "Outgoing_Call_Status": status,
                "Call_Result": result, "Call_Duration_in_seconds": secs, "Description": desc,
                "Call_Type": ctype, "Owner": who_owner or owner}

    calls_open = [
        call("k1", "d1", "Scheduled Call: Automated Follow Up 2", rel(-281), "Overdue"),
        call("k2", "d2", "Scheduled Call: Quote Follow Up 1 - 24 Hours - Tummy tuck", rel(-191), "Overdue"),
        call("k3", "d3", "Call back: lunch", rel(-161), "Overdue"),
        call("k4", "d4", "Call scheduled with Daniela Cruz", rel(-86), "Overdue"),
        call("k5", "d5", "Call back after work", rel(19)),
        call("k6", "d6", "Call scheduled with Nicole Hart", rel(49)),
        call("k7", "d7", "Scheduled Call: Quote Follow Up 2 - 2 Days - Lipo 360", rel(79)),
        call("k8", "d8", "Scheduled Call: Automated Follow Up 1", rel(109)),
        call("k9", "d6", "Scheduled Call: Automated Follow Up 3", rel(139)),
    ]
    # Two more today only if the day has room before midnight.
    for i, m in enumerate((169, 199)):
        if (now_utc + timedelta(minutes=m)).astimezone(tz).date() == today:
            calls_open.append(call("k1%d" % i, "d1%d" % (i + 1), "Call scheduled with someone", rel(m)))
    history = [
        call("h1", "d2", "Outgoing call to Carmen Vega", rel(-4 * 1440), "Completed", "Reached", 720,
             "Likes the price. Wants to talk to her husband first. Asked about recovery time off work."),
        call("h2", "d2", "Outgoing call to Carmen Vega", rel(-6 * 1440 + 60), "Completed", "No Answer / Not Reached", 25),
        call("h3", "d1", "Outgoing call to Shante Wilson", rel(-1440), "Completed", "No Answer / Not Reached", 30),
        call("h4", "d3", "Outgoing call to Lauren Pierce", rel(-1440), "Completed", "Requested call back", 140,
             "Call me at lunch."),
        call("h5", "d5", "Outgoing call to Tanya Brooks", rel(-1440), "Completed", "Requested call back", 200,
             "Call me after work."),
        call("h6", "d6", "Outgoing call to Nicole Hart", rel(-2 * 1440), "Completed", "Reached", 840),
        call("h7", "d7", "Outgoing call to Brittany Cole", rel(-2 * 1440), "Completed", "Reached", 600,
             "Quote $7,900 sent. Comparing two practices."),
        call("h8", "d8", "Outgoing call to Yesenia Ortiz", rel(-300), "Completed", "Left Voicemail", 40),
    ]
    results = (["Reached"] * 9 + ["Requested call back"] * 3 + ["Consultation Completed"] * 3
               + ["No Answer / Not Reached"] * 4 + ["Left Voicemail"] * 2 + [None, "Pending"])
    calls_today = []
    for i, res in enumerate(results):
        did = ("d1", "d2", "d3", "d5", "d6", "d7", "d8")[i % 7]
        start = now_utc - timedelta(minutes=20 + i * 12)
        if start.astimezone(tz).date() != today:
            start = now_utc - timedelta(minutes=1 + i)
        secs = 95 + i * 7 if res in ("Reached", "Requested call back", "Consultation Completed", None, "Pending") else 30
        calls_today.append(call("t%d" % i, did, "Outgoing call to " + deals[did]["Contact_Name"]["name"],
                                start.astimezone(tz).isoformat(), "Completed", res, secs))
    return {
        "calls_open": calls_open,
        "calls_today": calls_today + [c for c in calls_open if (ts(c["Call_Start_Time"]) or now_utc).astimezone(tz).date() == today],
        "deals_new": [d for d in deals.values() if d["id"] in ("d4", "d9", "d10", "d11", "d12", "d13", "d14")],
        "deals_queue": [dict(deals["d15"], id="q1", Contact_Name={"name": "Paola Ruiz", "id": "cq1"},
                             Created_Time=rel(-3)),
                        dict(deals["d15"], id="q2", Contact_Name={"name": "Lucia Romero", "id": "cq2"},
                             Phone="5550133308", Created_Time=rel(-11), Procedure="BBL")],
        "deals": dict(deals, q1=dict(deals["d15"], id="q1", Contact_Name={"name": "Paola Ruiz", "id": "cq1"},
                                     Created_Time=rel(-3)),
                      q2=dict(deals["d15"], id="q2", Contact_Name={"name": "Lucia Romero", "id": "cq2"},
                              Phone="5550133308", Created_Time=rel(-11), Procedure="BBL")),
        "deal_calls": history,
        "contacts": {},
        "busy": [{"Call_Start_Time": rel(19), "Call_Duration_in_seconds": 900},
                 {"Call_Start_Time": rel(49), "Call_Duration_in_seconds": 900}],
        # the pipeline lists (sales)
        "pipe_deals": [
            {"id": "9000000001", "Deal_Name": "Gina Moss", "Contact_Name": {"name": "Gina Moss", "id": "c91"},
             "Phone": "5550144001", "Stage": "Call Scheduled", "Created_Time": rel(-20 * 1440), "Procedure": "BBL",
             "Location": "New York", "Owner": owner},
            {"id": "9000000002", "Deal_Name": "Iris Vale", "Contact_Name": {"name": "Iris Vale", "id": "c92"},
             "Phone": "5550144002", "Stage": "Quote Sent", "Created_Time": rel(-9 * 1440),
             "Quote_Sent_Date": rel(-3 * 1440), "Amount": 9400, "Procedure": "Tummy tuck", "Owner": owner},
            {"id": "9000000003", "Deal_Name": "June Park", "Contact_Name": {"name": "June Park", "id": "c93"},
             "Phone": "5550144003", "Stage": "Retainer Invoice Sent", "Created_Time": rel(-12 * 1440),
             "Retainer_Sent_Date": rel(-2 * 1440), "Retainer_Paid": False, "Procedure": "Lipo 360", "Owner": owner},
        ],
        "future_open": [],
        "rep_calls": [],
        "tasks": [{"id": "t1", "Subject": "Quote Follow Up 3 - 1 Week - Lipo 360", "Due_Date": now_local.date().isoformat(),
                   "What_Id": {"id": "9000000001", "name": "June Park"}, "Who_Id": None}],
        "replies": [{"phone": "5550144002", "dealId": "9000000002", "patientName": "Iris Vale",
                     "inboundAt": rel(-25), "lastText": "Is the price the same if I book this week?", "textCount": 1}],
    }
