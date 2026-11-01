"""Coordinator My Day: one surgical coordinator's Journey calls for today.

Danny, 2026-10-31: "daily actions for agents should be extremely ergonomic ...
provide a seamless way to provide a consistent experience." The reps' My Day
(my_day.py) is untouched; this is the coordinators' own variant, shown on
/my-day when the person is a coordinator (Zoho Team_Schedules, Team =
'Coordinator').

Pure, like my_day.py: give it the raw Zoho rows and the time, it returns the
page. Nothing here reads the network, so checks_coord_day.py drives every rule
with fixtures.

Where the rows come from (zoho_client.coord_world, one read for the floor):
  journeys   every Active Journey owned by a coordinator, with its contact's
             name and phone
  calls      every call on a Journey (any owner: billing calls count as
             contact) from CALLS_DAYS ago to AHEAD_DAYS ahead

The rules, in one place:

  Right now  the coordinator's open calls (Scheduled/Overdue) on Journeys that
             are due: their time is within DUE_SOON_MINUTES or already passed
             (up to LATE_DAYS back). P1 first, then P2, P3, P4; oldest first
             inside a priority.
  Up next    the first UP_NEXT_SHOW of Right now; when fewer are due, the next
             calls later today fill it.
  Later      open calls later today, in time order.
  Done       the coordinator's completed Journey calls today. One with no
             result written keeps the buttons.

  Priority   the P# the back-office planner put at the end of the subject
             ("Name · Pre-op confirm · Surg Nov 5 · P1"). Danny's P1 list wins
             over it: day-before, pre-op, post-op day 1, confirm surgery
             happened, call backs and missed calls are always P1, and so is a
             payment call when the deadline is 3 days away or passed with money
             owed. No P# and not on that list: labs, welcome, post-op week are
             P2, check-ins P3.

  Last real contact
             the later of the Journey's Last Real Contact field
             (Next_Follow_Up_Date1) and the latest talk on any call on the
             Journey. A talk (is_talk): never No Answer / Left Voicemail;
             otherwise a completed (or inbound) call with a reached result
             (Reached, Callback Requested, Rescheduled Call, Transferred), or
             with no result (blank, Pending, Other) and 2+ minutes.
  Attempts   completed tries on the Journey since that contact that did not
             reach the patient.

Scorecard (per coordinator, live Journeys = Surgery Preparation, Ready for
Surgery, Post-Op Care):
  Within contact limit   Journeys in Surgery Preparation or Ready for Surgery
                         with surgery today or later (Post-Op is left out):
                         last real contact within 30 days when surgery is 90+
                         days out, 14 at 31 to 90, 7 at 30 or less. The gap is
                         counted in calendar days, Eastern (today minus the
                         day of the last talk). Talks here also count calls on
                         the patient's Contact and on the Journey's Linked
                         Deal (sales, billing on the deal), not only calls on
                         the Journey (hub decision 10-31, so this matches the
                         evidence numbers).
  Reach rate             of the coordinator's Journey calls with a result in
                         the last REACH_DAYS days, the share that reached her.
  Payment deadlines covered
                         live Journeys with money owed and a deadline in the
                         next DEADLINE_DAYS days: reached in the last 7 days,
                         or a call booked before the deadline.
  Welcomes in 1 working day
                         Journeys created in the last WELCOME_DAYS days whose
                         window has closed: a talk by the end of the
                         coordinator's next working day after creation.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone

LATE_MINUTES = 15
LATE_DAYS = 7
DUE_SOON_MINUTES = 5
UP_NEXT_SHOW = 3
CALLS_DAYS = 31        # the 30-day contact limit needs the 31st day back too
AHEAD_DAYS = 8
REACH_DAYS = 7
DEADLINE_DAYS = 7
WELCOME_DAYS = 14
TALK_SECONDS = 45   # Danny 11-01: "technically anything over 45 seconds should be a real call" (was 120 with no result)
QUIET_SHOW = 12

ZOHO_JOURNEY_URL = "https://crm.zoho.com/crm/org771460636/tab/CustomModule13/%s"
LIVE_STAGES = ("Surgery Preparation", "Ready for Surgery", "Post-Op Care")
PRE_SURGERY_STAGES = ("Surgery Preparation", "Ready for Surgery")
JOURNEY_MODULES = ("CustomModule13", "Journeys")      # v6 and v8 names for $se_module

# Who is a coordinator when Team_Schedules cannot be read (same six as the back
# office's fallback table, 2026-10-31). Days off and shift hours, Eastern.
FALLBACK_SCHEDULE = [
    {"id": "5212466000473252001", "name": "Alex Morales", "off": ["Sat", "Sun"], "start": 13, "end": 22},
    {"id": "5212466000454324769", "name": "Judith Merlo", "off": ["Sat", "Sun"], "start": 10, "end": 19},
    {"id": "5212466000659999015", "name": "Oscar Caballero", "off": ["Sun", "Mon"], "start": 10, "end": 19, "sat": [10, 17]},
    {"id": "5212466000531206001", "name": "Jorge Mier", "off": ["Sat", "Sun"], "start": 12, "end": 21},
    {"id": "5212466000659898072", "name": "Ariel Ramirez", "off": ["Sat", "Sun"], "start": 10, "end": 19},
    {"id": "5212466001001101001", "name": "Johana Duron", "off": ["Fri", "Sat"], "start": 10, "end": 19, "sun": [10, 17]},
]
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# ── results (the one-tap buttons) ─────────────────────────────────────────
# key -> (label, Zoho Call_Result, group, needs). The back office's
# journey-retry books the retry after No Answer / Left Voicemail on its own.
RESULTS = [
    ("reached",        "Reached",            "Reached",                 "talked", ""),
    ("call_back",      "Wants a call back",  "Callback Requested",      "talked", "time"),
    ("rescheduling",   "Rescheduling",       "Rescheduled Call",        "talked", ""),
    ("no_answer",      "No answer",          "No Answer / Not Reached", "missed", ""),
    ("left_voicemail", "Left voicemail",     "Left Voicemail",          "missed", ""),
]
RESULT_KEYS = {k for k, *_ in RESULTS}
RESULT_VALUE = {k: v for k, _, v, _, _ in RESULTS}
RESULT_HINT = {
    "reached": "done, nothing else booked",
    "call_back": "pick the time she asked for",
    "rescheduling": "she wants a new surgery date",
    "no_answer": "the retry books itself",
    "left_voicemail": "the retry books itself",
}

NO_RESULT = {"", "-none-", "pending"}
REACH = {"reached", "callback requested", "rescheduled call", "transferred"}
NOT_REACHED = {"no answer / not reached", "left voicemail"}
OPEN = {"scheduled", "overdue"}
LOGGED = re.compile(r"^\s*(outbound|outgoing|inbound|incoming)\s+call\b", re.I)
PRIO_TAIL = re.compile(r"^\s*P([1-4])\s*$")
ATTEMPT = re.compile(r"^\s*attempt\s+(\d)\s*/\s*(\d)\s*:\s*", re.I)
BILLING = re.compile(r"^\s*billing call\s*-\s*", re.I)

# purpose kind -> (match on the purpose text, label, default priority)
KINDS = [
    ("day_before", re.compile(r"day[- ]before", re.I),               "Day before surgery", 1),
    ("preop",      re.compile(r"pre[- ]?op", re.I),                  "Pre-op confirm", 1),
    ("happened",   re.compile(r"confirm surgery happened", re.I),    "Confirm surgery happened", 1),
    ("postop1",    re.compile(r"post[- ]?op day 1\b", re.I),         "Post-op day 1", 1),
    ("callback",   re.compile(r"^\s*call ?back|missed call", re.I),  "Call back", 1),
    ("payment",    re.compile(r"payment|balance|billing|overdue \$", re.I), "Payment", 2),
    ("labs",       re.compile(r"labs|clearance", re.I),              "Labs + clearance", 2),
    ("welcome",    re.compile(r"welcome", re.I),                     "Welcome", 2),
    ("postop",     re.compile(r"post[- ]?op", re.I),                 "Post-op check", 2),
    ("priority",   re.compile(r"priority|catch[- ]up", re.I),        "Priority check-in", 2),
    ("checkin",    re.compile(r"check[- ]?in|follow[- ]?up", re.I),  "Check-in", 3),
]
ALWAYS_P1 = {"day_before", "preop", "happened", "postop1", "callback"}
KIND_LABEL = {k: l for k, _, l, _ in KINDS}


# ── helpers ───────────────────────────────────────────────────────────────
def ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_of(value):
    """'2026-11-05' (or a datetime string) -> date, else None."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def ref_id(v):
    return str(v.get("id") or "") if isinstance(v, dict) else (str(v) if v else "")


def digits10(value) -> str:
    d = "".join(ch for ch in str(value or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def fmt_phone(value) -> str:
    d = digits10(value)
    return "(%s) %s-%s" % (d[:3], d[3:6], d[6:]) if len(d) == 10 else ""


def clock(dt_local) -> str:
    return dt_local.strftime("%I:%M %p").lstrip("0")


def status_of(c) -> str:
    return (c.get("Outgoing_Call_Status") or "").strip().lower()


def result_of(c) -> str:
    r = (c.get("Call_Result") or "").strip()
    return "" if r.lower() in NO_RESULT else r


def secs_of(c) -> int:
    try:
        return int(float(c.get("Call_Duration_in_seconds") or 0))
    except (TypeError, ValueError):
        return 0


def is_journey_call(c) -> bool:
    return c.get("$se_module") in JOURNEY_MODULES


def is_talk(c) -> bool:
    """A real conversation (Danny 11-01, one rule for coordinators and sales;
    the same as the back office's lib/journey-retry isTalk and
    daily-kpis/evidence_daily.py real_talk). No Answer / Left Voicemail never
    is. Otherwise the call is completed (or inbound) and has a reached result
    (Reached / Callback Requested / Rescheduled Call / Transferred), or lasted
    longer than TALK_SECONDS (45) whatever else its result says. Before 11-01
    the bar was 120 s with no result. An inbound call is not a talk just for
    being inbound."""
    r = result_of(c).lower()
    if r in NOT_REACHED:
        return False
    inbound = bool(re.search(r"inbound|incoming", c.get("Call_Type") or "", re.I))
    if status_of(c) != "completed" and not inbound:
        return False
    if r in REACH:
        return True
    return secs_of(c) > TALK_SECONDS


def is_try(c) -> bool:
    """A completed outbound try that did not reach her."""
    if status_of(c) != "completed" or is_talk(c):
        return False
    r = result_of(c).lower()
    return r in NOT_REACHED or (r == "" and LOGGED.match(c.get("Subject") or "") is not None)


def journey_name(j) -> str:
    first = (j.get("Contact.First_Name") or "").strip()
    last = (j.get("Contact.Last_Name") or "").strip()
    if first or last:
        return (first + " " + last).strip()
    return (j.get("Name") or "").split(":")[0].strip() or "Patient"


def first_name(full) -> str:
    parts = (full or "").split()
    return parts[0].title() if parts else ""


def short_name(full) -> str:
    parts = (full or "").split()
    if not parts:
        return "Patient"
    return parts[0] if len(parts) == 1 else "%s %s." % (parts[0], parts[-1][0].upper())


def money(v) -> str:
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return ""
    return "${:,.0f}".format(n) if n > 0 else ""


def rel_days(n) -> str:
    if n is None:
        return ""
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return "in %d days" % n if n > 0 else "%d days ago" % -n


def late_label(delta: timedelta) -> str:
    m = int(delta.total_seconds() // 60)
    d, rem = divmod(m, 1440)
    h, mm = divmod(rem, 60)
    if d:
        return "%d d late" % d
    if h and mm:
        return "%d h %d m late" % (h, mm)
    if h:
        return "%d h late" % h
    return "%d m late" % mm


def contact_limit(days_out) -> int:
    """Danny's never-quiet rule: 30 days at 90+ out, 14 at 31-90, 7 at 30 or less."""
    if days_out is None:
        return 14
    if days_out > 90:
        return 30
    if days_out > 30:
        return 14
    return 7


# ── the subject ───────────────────────────────────────────────────────────
def parse_subject(subject):
    """{'purpose', 'prio' (int or None), 'attempt' ('2/2' or ''), 'retry'}.

    "Name · Pre-op confirm · Surg Nov 5 · P1" -> purpose "Pre-op confirm", P1
    "Name · Attempt 1/2: check-in · Surg Dec 4 · P3" -> "check-in", attempt 1/2
    "Billing call - Overdue $1,200 - Name" -> "Billing call"
    "Call back: Name" -> "Call back"
    """
    s = (subject or "").strip()
    prio, attempt = None, ""
    parts = [p.strip() for p in s.split("·")] if "·" in s else [s]
    if len(parts) > 1 and PRIO_TAIL.match(parts[-1]):
        prio = int(PRIO_TAIL.match(parts[-1]).group(1))
        parts = parts[:-1]
    if len(parts) > 1:
        purpose = parts[1]
        if PRIO_TAIL.match(purpose):          # "Name · P1": the planner's bare priority row
            prio = prio or int(PRIO_TAIL.match(purpose).group(1))
            purpose = ""
    elif BILLING.match(s):
        purpose = "Billing call" + (" (overdue)" if "overdue" in s.lower() else "")
    elif re.match(r"^\s*call ?back\b", s, re.I):
        purpose = "Call back"
    else:
        purpose = s if not LOGGED.match(s) else ""
    m = ATTEMPT.match(purpose)
    if m:
        attempt = "%s/%s" % (m.group(1), m.group(2))
        purpose = purpose[m.end():]
    retry = bool(re.search(r"\(retry\)", s, re.I))
    purpose = re.sub(r"\s*\(retry\)\s*", "", purpose).strip()
    if purpose:
        purpose = purpose[0].upper() + purpose[1:]
    return {"purpose": purpose, "prio": prio, "attempt": attempt, "retry": retry}


def kind_of(purpose, agenda="") -> str:
    for k, rx, _, _ in KINDS:
        if rx.search(purpose or ""):
            return k
    for k, rx, _, _ in KINDS:                 # "Name · P1" rows: the briefing says what it is
        if rx.search(agenda or ""):
            return k
    return "checkin"


def priority(kind, tail_prio, deadline_days, balance) -> int:
    if kind in ALWAYS_P1:
        return 1
    if kind == "payment" and balance and deadline_days is not None and deadline_days <= 3:
        return 1
    if tail_prio:
        return tail_prio
    return next((p for k, _, _, p in KINDS if k == kind), 3)


# ── text drafts (display only; the app never sends them) ──────────────────
DRAFT_NOTE = "Draft, Danny to approve. Copy it into your texting line; this page sends nothing."


def draft_text(kind, patient_first, me_first, surgery_label, deadline_label=""):
    """House style: short, ☺️, line breaks, one next step, no dashes."""
    hi = "Hi %s ☺️" % (patient_first or "there")
    me = "This is %s from Goals Plastic Surgery." % (me_first or "your coordinator")
    surg = surgery_label or "your surgery date"
    lines = {
        "welcome": [hi, "This is %s, your surgical coordinator at Goals ☺️" % (me_first or "your coordinator"),
                    "Congratulations on booking your surgery for %s!" % surg,
                    "What time works for a quick welcome call?"],
        "day_before": [hi, "Your surgery is tomorrow, %s." % surg,
                       "Please reply to confirm you are all set."],
        "preop": [hi, me, "Your surgery on %s is coming up." % surg,
                  "What time today works to go over your checklist?"],
        "happened": [hi, "I hope everything went well with your surgery!",
                     "Please reply and let me know how you are feeling."],
        "postop1": [hi, "Just checking in after your surgery.",
                    "How are you feeling today? Reply when you can."],
        "postop": [hi, "Checking in on your recovery.",
                   "How are you feeling this week? Reply when you can."],
        "payment": [hi, me, "Your next payment is due %s." % (deadline_label or "soon"),
                    "What time works for a quick call to go over it?"],
        "labs": [hi, me, "We still need your labs and medical clearance for %s." % surg,
                 "Can you send them over this week?"],
        "callback": [hi, me, "Sorry I missed you!", "What time works best to call you back?"],
        "priority": [hi, me, "I would love to catch up before your surgery on %s." % surg,
                     "What time works for a quick call?"],
        "checkin": [hi, me, "Just checking in ahead of your surgery on %s." % surg,
                    "What time works for a quick call?"],
    }
    return "\n".join(lines.get(kind) or lines["checkin"])


def draft_missed(patient_first, me_first):
    return "\n".join(["Hi %s ☺️" % (patient_first or "there"),
                      "This is %s from Goals Plastic Surgery. I just tried calling you." % (me_first or "your coordinator"),
                      "What time works best to call you back?"])


# ── schedules ─────────────────────────────────────────────────────────────
def _hours(text):
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", str(text or ""))
    return [int(m.group(1)), int(m.group(2))] if m else None


def schedules_from_rows(rows):
    """Zoho Team_Schedules rows (Team = 'Coordinator') -> [{id, name, off, start,
    end, sat, sun, out_from, out_to}]. Inactive rows are left out. Empty or None
    -> the fallback six."""
    out = []
    for r in rows or []:
        if r.get("Active") is False or (r.get("Team") and r.get("Team") != "Coordinator"):
            continue
        uid = ref_id(r.get("Staff_User"))
        if not uid:
            continue
        off = r.get("Days_Off") or []
        out.append({"id": uid, "name": (r.get("Name") or "").strip(),
                    "off": [d[:3] for d in off if isinstance(d, str)],
                    "start": int(r.get("Start_Hour") or 10), "end": int(r.get("End_Hour") or 19),
                    "sat": _hours(r.get("Saturday_Hours")), "sun": _hours(r.get("Sunday_Hours")),
                    "out_from": r.get("Out_From"), "out_to": r.get("Out_To")})
    return out or [dict(s) for s in FALLBACK_SCHEDULE]


def works_on(sched, d: date) -> bool:
    if not sched:
        return d.weekday() < 5
    if WEEKDAYS[d.weekday()] in (sched.get("off") or []):
        return False
    a, b = day_of(sched.get("out_from")), day_of(sched.get("out_to"))
    if a and b and a <= d <= b:
        return False
    return True


def shift_on(sched, d: date):
    """(start hour, end hour) on day d, or None when off."""
    if not works_on(sched, d):
        return None
    wd = WEEKDAYS[d.weekday()]
    if wd == "Sat" and sched and sched.get("sat"):
        return tuple(sched["sat"])
    if wd == "Sun" and sched and sched.get("sun"):
        return tuple(sched["sun"])
    return (int((sched or {}).get("start") or 10), int((sched or {}).get("end") or 19))


def next_working_day(sched, d: date) -> date:
    for i in range(1, 15):
        x = d + timedelta(days=i)
        if works_on(sched, x):
            return x
    return d + timedelta(days=1)


def hour_label(h) -> str:
    h = int(h) % 24
    return "%d %s" % (12 if h % 12 == 0 else h % 12, "AM" if h < 12 else "PM")


def free_slots(sched, busy_utc, now_utc, tz, days=3, step=10, limit_per_day=18):
    """Open 10-minute slots in the coordinator's shift over her next `days`
    working days, at least 15 minutes away and 10 minutes clear of her booked
    calls."""
    out = []
    earliest = now_utc + timedelta(minutes=15)
    d0 = now_utc.astimezone(tz).date()
    seen_days = 0
    for i in range(0, 14):
        d = d0 + timedelta(days=i)
        sh = shift_on(sched, d)
        if not sh:
            continue
        n = 0
        t = datetime.combine(d, dtime(hour=sh[0]), tz)
        stop = datetime.combine(d, dtime(hour=min(sh[1], 23), minute=59 if sh[1] >= 24 else 0), tz)
        while t < stop and n < limit_per_day:
            tu = t.astimezone(timezone.utc)
            if tu >= earliest and all(abs((tu - b).total_seconds()) >= 600 for b in busy_utc):
                out.append({"at": tu.isoformat(), "day": "Today" if d == d0 else t.strftime("%a %b %-d"),
                            "label": clock(t)})
                n += 1
            t += timedelta(minutes=step)
        if n:
            seen_days += 1
        if seen_days >= days:
            break
    return out


# ── the page ──────────────────────────────────────────────────────────────
class Index:
    """The floor's Journeys and Journey calls, indexed once and shared by every
    coordinator's page and the floor view."""

    def __init__(self, world, now_utc, tz):
        self.now, self.tz = now_utc, tz
        self.today = now_utc.astimezone(tz).date()
        self.journeys = {str(j.get("id")): j for j in (world.get("journeys") or []) if j.get("id")}
        self.calls = [c for c in (world.get("calls") or []) if is_journey_call(c)]
        self.by_j = {}
        for c in self.calls:
            jid = ref_id(c.get("What_Id"))
            if jid:
                self.by_j.setdefault(jid, []).append(c)
        for v in self.by_j.values():
            v.sort(key=lambda c: ts(c.get("Call_Start_Time")) or now_utc)
        # For the contact-limit measure only: the Journey's calls plus calls
        # on the patient's Contact (Who_Id) and on the Journey's Linked Deal
        # (What_Id), one copy per call id. Booked-call checks, tries, rows,
        # deadlines and welcomes stay on the Journey's own calls.
        by_contact, by_deal = {}, {}
        for jid, j in self.journeys.items():
            if ref_id(j.get("Contact")):
                by_contact.setdefault(ref_id(j.get("Contact")), []).append(jid)
            if ref_id(j.get("Deal")):
                by_deal.setdefault(ref_id(j.get("Deal")), []).append(jid)
        self.wide_by_j = {jid: list(v) for jid, v in self.by_j.items()}
        seen = {jid: {str(c.get("id")) for c in v} for jid, v in self.by_j.items()}
        for key, table, field in (("contact_calls", by_contact, "Who_Id"), ("deal_calls", by_deal, "What_Id")):
            for c in world.get(key) or []:
                cid = str(c.get("id") or "")
                for jid in table.get(ref_id(c.get(field)), ()):
                    got = seen.setdefault(jid, set())
                    if cid and cid in got:
                        continue
                    got.add(cid)
                    self.wide_by_j.setdefault(jid, []).append(c)
        self.wide = world.get("contact_calls") is not None or world.get("deal_calls") is not None

    def last_talk(self, jid, wide=False):
        """(when, by owner id) of the latest real contact, or (None, '').
        wide: also the calls on the patient's Contact and Linked Deal."""
        j = self.journeys.get(jid) or {}
        best, by = ts(j.get("Next_Follow_Up_Date1")), ""
        for c in (self.wide_by_j if wide else self.by_j).get(jid, ()):
            t = ts(c.get("Call_Start_Time"))
            if t and t <= self.now and is_talk(c) and (best is None or t > best):
                best, by = t, ref_id(c.get("Owner"))
        return best, by

    def tries_since(self, jid, since):
        n = 0
        for c in self.by_j.get(jid, ()):
            t = ts(c.get("Call_Start_Time"))
            if t and t <= self.now and (since is None or t > since) and is_try(c):
                n += 1
        return n

    def booked_between(self, jid, a, b):
        for c in self.by_j.get(jid, ()):
            t = ts(c.get("Call_Start_Time"))
            if t and a <= t <= b and status_of(c) in OPEN:
                return True
        return False

    def days_out(self, j):
        s = day_of(j.get("Surgery_Date"))
        return (s - self.today).days if s else None


def _when_label(t_local, today):
    return clock(t_local) if t_local.date() == today else t_local.strftime("%b %-d, ") + clock(t_local)


def make_row(ix, c, coord_first, kind_hint=""):
    now, tz, today = ix.now, ix.tz, ix.today
    jid = ref_id(c.get("What_Id"))
    j = ix.journeys.get(jid) or {}
    name = journey_name(j) if j else ((c.get("Subject") or "").split("·")[0].strip() or "Patient")
    sp = parse_subject(c.get("Subject"))
    agenda = (c.get("Call_Agenda") or "").strip()
    kind = kind_hint or kind_of(sp["purpose"], agenda)
    balance = j.get("Journey_Balance_Due")
    try:
        balance = float(balance or 0)
    except (TypeError, ValueError):
        balance = 0.0
    dl = day_of(j.get("Fourth_Attempt_Date_15D"))
    dl_days = (dl - today).days if dl else None
    prio = priority(kind, sp["prio"], dl_days if balance > 0 else None, balance)
    surg = day_of(j.get("Surgery_Date"))
    days_out = (surg - today).days if surg else None
    t = ts(c.get("Call_Start_Time"))
    t_local = t.astimezone(tz) if t else None
    last, last_by = ix.last_talk(jid) if jid else (None, "")
    tries = ix.tries_since(jid, last) if jid else 0
    phone = digits10(j.get("Contact.Mobile") or j.get("Contact.Phone"))
    surgery_label = surg.strftime("%A, %b %-d") if surg else ""
    deadline_label = dl.strftime("%A, %b %-d") if dl else ""
    pfirst = first_name(name)
    r = {
        "key": str(c.get("id") or ""), "call_id": str(c.get("id") or ""), "journey_id": jid,
        "contact_id": ref_id(j.get("Contact")),
        "name": name, "short": short_name(name), "first": pfirst,
        "purpose": sp["purpose"] or ("Phone call" if LOGGED.match(c.get("Subject") or "")
                                     else KIND_LABEL.get(kind, "Check-in")), "kind": kind,
        "kind_label": KIND_LABEL.get(kind, "Check-in"),
        "prio": prio, "attempt": sp["attempt"], "retry": sp["retry"],
        "start": t.isoformat() if t else "",
        "when": _when_label(t_local, today) if t_local else "",
        "sub": "", "sub_kind": "",
        "status": status_of(c), "result": result_of(c),
        "stage": j.get("Journey_Stage") or "",
        "surgery": surg.strftime("%a %b %-d") if surg else "not set",
        "surgery_long": surgery_label, "days_out": days_out,
        "days_out_label": rel_days(days_out) if days_out is not None else "",
        "balance": balance, "balance_label": money(balance) or "paid",
        "deadline": dl.strftime("%b %-d") if dl else "", "deadline_days": dl_days,
        "deadline_label": (rel_days(dl_days) if dl_days is not None else ""),
        "labs": j.get("Lab_Result_Status") or "not set",
        "clearance": j.get("Clearance_Status") or "not set",
        "last_contact": (last.astimezone(tz).strftime("%b %-d") if last else ""),
        "last_days": ((now - last).days if last else None),
        "last_label": ("%s (%s)" % (last.astimezone(tz).strftime("%b %-d"),
                                    "today" if (now - last).days == 0 else "%d d ago" % (now - last).days)
                       if last else "not reached in %d days" % CALLS_DAYS),
        "tries": tries,
        "tries_label": ("%d tr%s since" % (tries, "y" if tries == 1 else "ies")) if tries else "",
        "briefing": agenda[:400],
        "note": (c.get("Description") or "").strip()[:1200],
        "phone": phone, "phone_label": fmt_phone(phone),
        "zoho": (ZOHO_JOURNEY_URL % jid) if jid else "",
        "draft": draft_text(kind, pfirst, coord_first, surgery_label, deadline_label),
        "draft_missed": draft_missed(pfirst, coord_first),
    }
    if t and status_of(c) in OPEN:
        mins = (t - now).total_seconds() / 60
        if mins < -LATE_MINUTES:
            r["sub"], r["sub_kind"] = late_label(now - t), "late"
        elif mins <= DUE_SOON_MINUTES:
            r["sub"], r["sub_kind"] = "now", "soon"
        elif mins <= 60:
            r["sub"], r["sub_kind"] = "in %d min" % int(mins), "soon"
    return r


def build(world, now_utc, tz, coord, schedules=None, index=None):
    """One coordinator's page. coord = {id, name}. schedules: from
    schedules_from_rows. index: a shared Index (the floor builds one)."""
    ix = index or Index(world, now_utc, tz)
    cid = str((coord or {}).get("id") or "")
    sched = next((s for s in (schedules or []) if s["id"] == cid), None)
    cname = (coord or {}).get("name") or (sched or {}).get("name") or ""
    cfirst = first_name(cname)
    now_local = now_utc.astimezone(tz)
    today = ix.today
    end_today = datetime.combine(today + timedelta(days=1), dtime(), tz).astimezone(timezone.utc)
    late_from = now_utc - timedelta(days=LATE_DAYS)

    mine = [c for c in ix.calls if ref_id(c.get("Owner")) == cid]
    open_now, later, done = [], [], []
    for c in mine:
        t = ts(c.get("Call_Start_Time"))
        if not t or (c.get("Call_Type") or "Outbound") != "Outbound":
            if t and status_of(c) == "completed" and t.astimezone(tz).date() == today:
                done.append(c)
            continue
        st = status_of(c)
        if st in OPEN and not LOGGED.match(c.get("Subject") or ""):
            if late_from <= t <= now_utc + timedelta(minutes=DUE_SOON_MINUTES):
                open_now.append(c)
            elif now_utc < t < end_today:
                later.append(c)
        elif st == "completed" and t.astimezone(tz).date() == today:
            done.append(c)

    def one_per_journey(rows):
        out, seen = [], set()
        for r in rows:
            k = r["journey_id"] or r["call_id"]
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
        return out

    now_rows = [make_row(ix, c, cfirst) for c in open_now]
    now_rows.sort(key=lambda r: (r["prio"], r["start"]))
    now_rows = one_per_journey(now_rows)
    now_ids = {r["journey_id"] for r in now_rows}
    later_rows = [make_row(ix, c, cfirst) for c in later]
    later_rows.sort(key=lambda r: r["start"])
    later_rows = [r for r in one_per_journey(later_rows) if r["journey_id"] not in now_ids]
    done_rows = [make_row(ix, c, cfirst) for c in done]
    done_rows.sort(key=lambda r: r["start"], reverse=True)

    up = [dict(r, key="up" + r["key"]) for r in now_rows[:UP_NEXT_SHOW]]
    if len(up) < UP_NEXT_SHOW:
        up += [dict(r, key="up" + r["key"]) for r in later_rows[:UP_NEXT_SHOW - len(up)]]

    summary = {}
    for r in done_rows:
        k = r["result"] or "No result yet"
        summary[k] = summary.get(k, 0) + 1

    sh = shift_on(sched, today) if sched else None
    page = {
        "coord": {"id": cid, "name": cname, "first": cfirst},
        "role": "coordinator",
        "now": now_utc.isoformat(), "now_label": clock(now_local),
        "date_label": now_local.strftime("%A, %b %-d"),
        "shift": ("%s to %s" % (hour_label(sh[0]), hour_label(sh[1]))) if sh else ("Off today" if sched else ""),
        "up_next": up,
        "right_now": now_rows, "later": later_rows, "done": done_rows,
        "done_summary": sorted(({"label": k, "n": n} for k, n in summary.items()),
                               key=lambda x: (x["label"] == "No result yet", -x["n"])),
        "nums": {"right_now": len(now_rows), "late": sum(1 for r in now_rows if r["sub_kind"] == "late"),
                 "p1": sum(1 for r in now_rows if r["prio"] == 1), "later": len(later_rows),
                 "done": len(done_rows), "no_result": sum(1 for r in done_rows if not r["result"]),
                 "reached": sum(1 for r in done_rows if r["result"].lower() in REACH)},
        "results": [{"key": k, "label": l, "value": v, "group": g, "needs": n, "hint": RESULT_HINT.get(k, "")}
                    for k, l, v, g, n in RESULTS],
        "draft_note": DRAFT_NOTE,
        "score": scorecard(ix, cid, sched),
    }
    return page


def scorecard(ix, cid, sched=None):
    now, tz, today = ix.now, ix.tz, ix.today

    def pct(n, of):
        return round(100 * n / of) if of else None

    live = [j for j in ix.journeys.values()
            if ref_id(j.get("Owner")) == cid and (j.get("Journey_Stage") or "") in LIVE_STAGES]
    # The contact limit counts patients still before surgery (hub decision
    # 10-31): Surgery Preparation or Ready for Surgery, surgery today or later.
    pre = [j for j in live if (j.get("Journey_Stage") or "") in PRE_SURGERY_STAGES
           and ix.days_out(j) is not None and ix.days_out(j) >= 0]

    # 1. within the contact limit
    within, quiet = 0, []
    for j in pre:
        jid = str(j.get("id"))
        do = ix.days_out(j)
        lim = contact_limit(do)
        last, _ = ix.last_talk(jid, wide=True)
        gap = (today - last.astimezone(tz).date()).days if last else None
        if gap is not None and gap <= lim:
            within += 1
        else:
            booked = ix.booked_between(jid, now - timedelta(days=LATE_DAYS), now + timedelta(days=AHEAD_DAYS))
            name = journey_name(j)
            quiet.append({"journey_id": jid, "name": name, "short": short_name(name),
                          "zoho": ZOHO_JOURNEY_URL % jid, "limit": lim,
                          "days_out": do, "days_out_label": rel_days(do) if do is not None else "no date",
                          "last": last.astimezone(tz).strftime("%b %-d") if last else "",
                          "gap": gap, "over": (gap - lim) if gap is not None else None, "booked": booked})
    quiet.sort(key=lambda q: (q["booked"], -(q["over"] if q["over"] is not None else 999),
                              q["days_out"] if q["days_out"] is not None else 999))

    # 2. reach rate
    since = now - timedelta(days=REACH_DAYS)
    with_result = reached = 0
    for c in ix.calls:
        if ref_id(c.get("Owner")) != cid or status_of(c) != "completed":
            continue
        t = ts(c.get("Call_Start_Time"))
        if not t or t < since or t > now:
            continue
        r = result_of(c).lower()
        if r in REACH or r in NOT_REACHED:
            with_result += 1
            reached += 1 if r in REACH else 0

    # 3. payment deadlines covered
    due, covered, missing = 0, 0, []
    for j in live:
        try:
            bal = float(j.get("Journey_Balance_Due") or 0)
        except (TypeError, ValueError):
            bal = 0.0
        dl = day_of(j.get("Fourth_Attempt_Date_15D"))
        if bal <= 0 or not dl or not (today <= dl <= today + timedelta(days=DEADLINE_DAYS)):
            continue
        jid = str(j.get("id"))
        due += 1
        last, _ = ix.last_talk(jid)
        dl_end = datetime.combine(dl, dtime(23, 59), tz).astimezone(timezone.utc)
        day0 = datetime.combine(today, dtime(), tz).astimezone(timezone.utc)
        if (last and now - last <= timedelta(days=7)) or ix.booked_between(jid, day0, dl_end):
            covered += 1
        else:
            name = journey_name(j)
            missing.append({"journey_id": jid, "short": short_name(name), "name": name,
                            "zoho": ZOHO_JOURNEY_URL % jid, "deadline": dl.strftime("%b %-d"),
                            "balance": money(bal)})

    # 4. welcomes within 1 working day
    w_due = w_ok = w_open = 0
    late_welcomes = []
    for j in ix.journeys.values():
        if ref_id(j.get("Owner")) != cid:
            continue
        created = ts(j.get("Created_Time"))
        if not created or created < now - timedelta(days=WELCOME_DAYS):
            continue
        jid = str(j.get("id"))
        cd = created.astimezone(tz).date()
        nwd = next_working_day(sched, cd)
        sh = shift_on(sched, nwd) or (10, 19)
        deadline = datetime.combine(nwd, dtime(hour=min(sh[1], 23), minute=59), tz).astimezone(timezone.utc)
        lrc = ts(j.get("Next_Follow_Up_Date1"))
        talked = bool(lrc and created <= lrc <= deadline) or any(
            is_talk(c) and created <= (ts(c.get("Call_Start_Time")) or now) <= deadline
            for c in ix.by_j.get(jid, ()))
        if deadline > now:
            if not talked:
                w_open += 1
            else:
                w_due += 1
                w_ok += 1
            continue
        w_due += 1
        if talked:
            w_ok += 1
        else:
            name = journey_name(j)
            late_welcomes.append({"journey_id": jid, "short": short_name(name), "name": name,
                                  "zoho": ZOHO_JOURNEY_URL % jid,
                                  "created": created.astimezone(tz).strftime("%b %-d")})

    return {
        "live": len(live),
        "contact": {"n": within, "of": len(pre), "pct": pct(within, len(pre)), "goal": 100,
                    "quiet": quiet[:QUIET_SHOW], "quiet_n": len(quiet), "wide": ix.wide},
        "reach": {"n": reached, "of": with_result, "pct": pct(reached, with_result), "days": REACH_DAYS},
        "deadlines": {"n": covered, "of": due, "pct": pct(covered, due), "goal": 100, "days": DEADLINE_DAYS,
                      "missing": missing[:QUIET_SHOW]},
        "welcomes": {"n": w_ok, "of": w_due, "pct": pct(w_ok, w_due), "goal": 100, "open": w_open,
                     "days": WELCOME_DAYS, "late": late_welcomes[:QUIET_SHOW]},
    }


def floor(world, now_utc, tz, schedules):
    """Every coordinator side by side, for managers."""
    ix = Index(world, now_utc, tz)
    rows = []
    for s in schedules or []:
        p = build(world, now_utc, tz, {"id": s["id"], "name": s["name"]}, schedules, index=ix)
        rows.append({"id": s["id"], "name": s["name"], "shift": p["shift"], "nums": p["nums"],
                     "score": {k: {kk: vv for kk, vv in v.items() if kk not in ("quiet", "missing", "late")}
                               if isinstance(v, dict) else v for k, v in p["score"].items()}})
    return {"now_label": clock(now_utc.astimezone(tz)),
            "date_label": now_utc.astimezone(tz).strftime("%A, %b %-d"), "rows": rows}


def patch_call(world, call_id, fields):
    """Apply a write to the cached rows so the page shows it before the next read."""
    for c in world.get("calls") or []:
        if str(c.get("id")) == str(call_id):
            c.update(fields)
            return True
    return False


# ── fake data (local screenshots and checks; never on in production) ──────
FAKE_SCHEDULE = [{"id": "u-jorge", "name": "Jorge Mier", "off": [], "start": 0, "end": 24}]


def fake_world(now_utc, tz, coord_id="u-jorge"):
    """A coordinator's day: three due (one late P1), two later, three done.
    Made-up patients, 555 numbers."""
    today = now_utc.astimezone(tz).date()

    def rel(minutes):
        return (now_utc + timedelta(minutes=minutes)).isoformat()

    def d(n):
        return (today + timedelta(days=n)).isoformat()

    people = [("j1", "Carmen", "Vega", 1, 0, None, "Approved", "Approved", -2),
              ("j2", "Lauren", "Pierce", 9, 2400, 2, "In Review", "Awaiting Submission", -12),
              ("j3", "Daniela", "Cruz", 48, 5000, 14, "Not Received", None, None),
              ("j4", "Tanya", "Brooks", -1, 0, None, "Approved", "Approved", -5),
              ("j5", "Nicole", "Hart", 120, 3800, 70, "Not Received", None, -40),
              ("j6", "Ana", "Reyes", 21, 1200, -1, "Approved", "Received - Pending Review", -3)]
    journeys = []
    for jid, fn, ln, out, bal, dl, labs, mc, lrc in people:
        journeys.append({"id": jid, "Name": "%s %s" % (fn, ln), "Surgery_Date": d(out),
                         "Journey_Stage": "Post-Op Care" if out < 0 else "Surgery Preparation", "Case_Status": "Active",
                         "Journey_Balance_Due": bal, "Fourth_Attempt_Date_15D": (d(dl) + "T09:00:00-04:00") if dl is not None else None,
                         "Lab_Result_Status": labs, "Clearance_Status": mc,
                         "Next_Follow_Up_Date1": rel(lrc * 1440) if lrc is not None else None,
                         "Owner": {"id": coord_id}, "Created_Time": rel(-60 * 1440) if jid != "j3" else rel(-2 * 1440),
                         "Contact": {"id": "c" + jid}, "Contact.First_Name": fn, "Contact.Last_Name": ln,
                         "Contact.Mobile": "555012%04d" % (int(jid[1:]) * 7)})
    names = {j["id"]: j["Name"] for j in journeys}

    def call(cid, jid, purpose, start_min, status="Scheduled", result=None, prio="P2", secs=None, agenda=None):
        subj = "%s · %s · Surg %s · %s" % (names[jid], purpose, "Nov 5", prio) if purpose else "Outgoing call to " + names[jid]
        return {"id": cid, "Subject": subj, "Call_Type": "Outbound", "Outgoing_Call_Status": status,
                "Call_Start_Time": rel(start_min), "Call_Duration_in_seconds": secs, "Call_Result": result,
                "Call_Agenda": agenda, "Description": "GOAL: %s\nDONE WHEN: you spoke with the patient." % purpose if purpose else None,
                "What_Id": {"id": jid}, "Owner": {"id": coord_id}, "$se_module": "CustomModule13"}

    calls = [
        call("k1", "j1", "Day-before confirm", -50, "Overdue", prio="P1",
             agenda="P1 · Day-before · surg tomorrow · ASK: arrival time, ride home, NPO"),
        call("k2", "j2", "Full balance due " + (today + timedelta(days=2)).strftime("%b %-d"), -3, prio="P2"),
        call("k3", "j3", "Welcome", 2, prio="P2"),
        call("k4", "j5", "Check-in", 90, prio="P3"),
        call("k5", "j6", "Labs + clearance due Nov 9", 150, prio="P2"),
        call("h1", "j4", "Post-op day 1", -180, "Completed", "Reached", "P1", secs=420),
        call("h2", "j6", "", -120, "Completed", "No Answer / Not Reached", secs=30),
        call("h3", "j5", "Attempt 1/2: check-in", -60, "Completed", None, "P3", secs=15),
        call("h4", "j1", "", -3 * 1440, "Completed", "Reached", secs=300),
    ]
    return {"journeys": journeys, "calls": calls}
