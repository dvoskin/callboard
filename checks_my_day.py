"""My Day (build plan step 21): one rep's calls for today, from Zoho.

Danny approved the mock-up on 2026-10-20 and answered its questions: My Day
lives in the Call Tracker; managers open any rep's page; "late" is 15 minutes
past the call time; the rep sees "talked, nothing booked"; coordinators get
their own buttons later. This check fails when one of those rules, or the
result-button proxy's safety rules, is broken.
Run: ./.venv/bin/python -B checks_my_day.py   (tally LAST)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import my_day as M  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 20, 14, 41, tzinfo=TZ).astimezone(timezone.utc)   # a Tuesday


def at(minutes):
    return (NOW + timedelta(minutes=minutes)).astimezone(TZ).isoformat()


def deal(did, name, created_min, stage="New Lead", phone="5550190042"):
    return {"id": did, "Contact_Name": {"id": "c" + did, "name": name}, "Phone": phone, "Stage": stage,
            "Created_Time": at(created_min), "Procedure": "BBL", "Location": "New York", "Language": "English",
            "Lead_Source": "Instagram", "Amount": None}


def call(cid, did, subject, start_min, status="Scheduled", result=None, secs=None, ctype="Outbound", desc=None):
    return {"id": cid, "What_Id": {"id": did} if did else None, "Who_Id": None, "Subject": subject,
            "Call_Start_Time": at(start_min), "Outgoing_Call_Status": status, "Call_Result": result,
            "Call_Duration_in_seconds": secs, "Call_Type": ctype, "Description": desc,
            "Owner": {"id": "u1", "name": "Rep"}}


DEALS = {d["id"]: d for d in [
    deal("d1", "Shante Wilson", -3 * 1440), deal("d2", "Carmen Vega", -6 * 1440),
    deal("d3", "Lauren Pierce", -2 * 1440), deal("d4", "Tanya Brooks", -4 * 1440),
    deal("d5", "Nicole Hart", -5 * 1440), deal("n1", "Ana Reyes", -6), deal("n2", "Monique Davis", -22),
    deal("n3", "Old Lead", -25 * 60), deal("n4", "Called Lead", -40), deal("n5", "Gone Lead", -30, stage="Unsubscribe"),
    deal("t1", "Talk Done", -9 * 1440), deal("t2", "Talk Booked", -9 * 1440), deal("t3", "Short Talk", -9 * 1440),
]}
OPEN = [
    call("late-old", "d1", "Scheduled Call: Automated Follow Up 2", -281, "Overdue"),
    call("late-q", "d2", "Scheduled Call: Quote Follow Up 1 - 24 Hours - BBL", -191, "Overdue"),
    call("late-16", "d3", "Call back at lunch", -16),
    call("today-14", "d4", "Call scheduled with Tanya", -14),
    call("today-later", "d5", "Call scheduled with Nicole", 90),
    call("today-soon", "n4", "MISSED call follow up", 30),
    call("cancelled", "d1", "Scheduled Call: Automated Follow Up 3", -100, "Cancelled"),
    call("tomorrow", "d2", "Scheduled Call: Quote Follow Up 2", 60 * 24, "Scheduled"),
]
TODAY = [
    call("done-noresult", "d1", "Outgoing call to Shante", -300, "Completed", None, 30),
    call("done-pending", "d2", "Outgoing call to Carmen", -280, "Completed", "Pending", 45),
    call("done-reached", "t1", "Outgoing call to Talk Done", -200, "Completed", "Reached", 200),
    call("done-booked", "t2", "Outgoing call to Talk Booked", -190, "Completed", "Reached", 300),
    call("t2-next", "t2", "Call scheduled with Talk Booked", 60 * 26, "Scheduled"),
    call("done-short", "t3", "Outgoing call to Short Talk", -180, "Completed", "Reached", 59),
    call("done-noanswer", "d3", "Outgoing call to Lauren", -170, "Completed", "No Answer / Not Reached", 80),
    call("done-n4", "n4", "Outgoing call to Called Lead", -20, "Completed", "Left Voicemail", 30),
    call("auto", "d5", "Follow-up Call", -150, "Completed", None, 20, desc="Auto-completed by workflow"),
    call("logged-open", "d5", "Outgoing call to Nicole", -100, "Scheduled"),
] + [c for c in OPEN if c["id"].startswith("today")]
WORLD = {"calls_open": OPEN, "calls_today": TODAY, "deals": DEALS,
         "deals_new": [DEALS[k] for k in ("n1", "n2", "n3", "n4", "n5")],
         "deals_queue": [deal("q1", "Queue Lead", -3)],
         "deal_calls": [call("h1", "d2", "Outgoing call to Carmen", -4 * 1440, "Completed", "Reached", 720,
                             desc="Likes the price. Wants to talk to her husband first."),
                        call("h2", "d2", "Scheduled Call: Quote Follow Up 1", -4 * 1440 - 10, "Completed", None, 300,
                             desc="BEFORE YOU CALL - check: patient name")],
         "contacts": {}}
P = M.build(WORLD, NOW, TZ, role="sales", rep={"id": "u1", "name": "Jessica Morales"}, queue_enabled=True)

# ---- late: 15 minutes past, oldest first, cancelled never ----
late_ids = [r["call_id"] for r in P["late"]]
ck("late is 15 minutes past the call time: 16 min ago is late, 14 min ago is not",
   "late-16" in late_ids and "today-14" not in late_ids, late_ids)
ck("late is oldest first", late_ids == ["late-old", "late-q", "late-16"], late_ids)
ck("a cancelled call is on no list", all(r["call_id"] != "cancelled" for r in P["late"] + P["today"]), late_ids)
ck("a logged call left open is not a call to make", all(r["call_id"] != "logged-open" for r in P["late"] + P["today"]),
   [r["call_id"] for r in P["today"]])
ck("the late count in the header is the late list", P["nums"]["late"] == 3, P["nums"])
ck("a late row says how late", P["late"][0]["sub"] == "4 h 41 m late" and P["late"][0]["sub_kind"] == "late", P["late"][0])
ck("M.LATE_MINUTES is 15 (Danny's answer)", M.LATE_MINUTES == 15, M.LATE_MINUTES)

# ---- today: time order, the Now line, nothing from tomorrow ----
today_ids = [r["call_id"] for r in P["today"]]
ck("today is in time order and stops at midnight", today_ids == ["today-14", "today-soon", "today-later"], today_ids)
ck("the Now line sits after the calls whose time has passed", P["now_index"] == 1, P["now_index"])

# ---- the why pill ----
pill = {r["call_id"]: r["why"] for r in P["late"] + P["today"]}
ck("'Quote Follow Up' in the subject is a quote follow-up", pill.get("late-q") == "quote", pill)
ck("'Automated Follow Up' is no answer, try again", pill.get("late-old") == "retry", pill)
ck("'MISSED' is no answer, try again", pill.get("today-soon") == "retry", pill)
ck("'call back' is asked for a call back", pill.get("late-16") == "cb", pill)
ck("anything else is a booked call", pill.get("today-later") == "booked", pill)

# ---- names: short on the list, full when opened ----
r2 = next(r for r in P["late"] if r["call_id"] == "late-q")
ck("the list shows first name and last initial", r2["short"] == "Carmen V." and r2["full"] == "Carmen Vega", r2["short"])
ck("the last call note is a rep's note, not the script Zoho puts on every call",
   r2["note"] and r2["note"]["text"].startswith("Likes the price"), r2["note"])
ck("the Call button dials the deal's number", r2["phone"] == "5550190042" and r2["phone_label"] == "(555) 019-0042", r2["phone"])

# ---- new leads ----
new_ids = [r["deal_id"] for r in P["new_leads"]]
ck("new leads: created in the last 24 h with no completed outbound call, newest first",
   new_ids == ["n1", "n2"], new_ids)
ck("a 25-hour-old deal is not a new lead", "n3" not in new_ids, new_ids)
ck("a deal already called (even to voicemail) is not a new lead", "n4" not in new_ids, new_ids)
ck("an unsubscribed deal is not a lead to call", "n5" not in new_ids, new_ids)
ck("new leads waiting counts the same rule", P["nums"]["new_leads"] == 2, P["nums"])
ck("the shared queue is listed with claim-on-call when it is on",
   [r["deal_id"] for r in P["queue"]] == ["q1"] and P["queue"][0].get("claim") is True, P["queue"])
# Zoho's quote ladder: booked for midnight, then marked Completed (5 min) by Zoho
# itself. Charlotte, 10-21: five "12:00 AM · no result" rows and a fake
# "Call, 5 min" in each history, next to follow-ups she had booked herself.
midnight = -(14 * 60 + 41)
RW = dict(WORLD, deals=dict(DEALS, r1=deal("r1", "Vanessa Garcia", -4 * 1440, stage="Quote Sent")),
          calls_today=list(TODAY) + [
              call("robot-q", "r1", "Scheduled Call: Quote Follow Up 1 - 24 Hours - Lower Blepharoplasty", midnight,
                   "Completed", None, 300)],
          calls_open=list(OPEN) + [call("r1-own", "r1", "talk with husband", 6 * 1440 + 200, "Scheduled")])
PR = M.build(RW, NOW, TZ, rep={"id": "u1", "name": "Jessica Morales"})
ck("a ladder call Zoho completed by itself is not a call with no result",
   "robot-q" not in [r["call_id"] for r in PR["no_result"]], [r["call_id"] for r in PR["no_result"]])
ck("...nor done today", PR["nums"]["done"] == P["nums"]["done"], (PR["nums"]["done"], P["nums"]["done"]))
ck("M.is_auto_completed: robot subject, completed, no result",
   M.is_auto_completed({"Subject": "Scheduled Call: Quote Follow Up 1 - 24 Hours - BBL", "Outgoing_Call_Status": "Completed"}), "robot")
ck("...but one with a result written is a real call",
   not M.is_auto_completed({"Subject": "Scheduled Call: Quote Follow Up 1 - 24 Hours - BBL", "Outgoing_Call_Status": "Completed",
                            "Call_Result": "Reached"}), "result")
ck("...and the rep's own dial record never is",
   not M.is_auto_completed({"Subject": "Outgoing call to Vanessa", "Outgoing_Call_Status": "Completed"}), "dial")
H = M.build(dict(RW, calls_open=list(RW["calls_open"]) + [call("r1-today", "r1", "Call scheduled with Vanessa", 60)]),
            NOW, TZ, rep={"id": "u1", "name": "x"})
hr = next((r for r in H["today"] if r["deal_id"] == "r1"), None)
ck("history: a call still ahead is marked 'up' (Coming up), past calls are not, and the row's own call is left out",
   hr is not None and [h["k"] for h in hr["history"] if h.get("up")] == ["Next call"]
   and not any(h["k"].startswith("Call,") for h in hr["history"]),
   hr and hr["history"])
# ---- Follow-ups due: Zoho tasks due today or the last 3 days, one per patient
_today = NOW.astimezone(TZ).date()
TW = dict(WORLD, pipe_deals=[dict(d, Owner={"id": "u1"}) for d in PIPE] if "PIPE" in globals() else [],
          future_open=[], rep_calls=[], replies=[],
          tasks=[{"id": "k1", "Subject": "Quote Follow Up 3 - 1 Week", "Due_Date": _today.isoformat(), "What_Id": {"id": "tq", "name": "Tia Q"}},
                 {"id": "k2", "Subject": "Quote Follow Up 2 - 72 Hours", "Due_Date": (_today - timedelta(days=4)).isoformat(), "What_Id": {"id": "tq", "name": "Tia Q"}},
                 {"id": "k3", "Subject": "Call back", "Due_Date": (_today - timedelta(days=2)).isoformat(), "What_Id": {"id": "tn", "name": "Nia N"}},
                 {"id": "k4", "Subject": "Old one", "Due_Date": (_today - timedelta(days=40)).isoformat(), "What_Id": {"id": "to", "name": "Old O"}},
                 {"id": "k5", "Subject": "Tomorrow", "Due_Date": (_today + timedelta(days=1)).isoformat(), "What_Id": {"id": "tt", "name": "Tom T"}}],
          task_deals={"tq": {"id": "tq", "Deal_Name": "Tia Q", "Stage": "Quote Sent", "Contact_Name": {"id": "c1", "name": "Tia Q"}},
                      "tn": {"id": "tn", "Deal_Name": "Nia N", "Stage": "Call Scheduled", "Contact_Name": {"id": "c2", "name": "Nia N"}}})
TP = M.build(TW, NOW, TZ, rep={"id": "u1", "name": "Jessica Morales"})["pipeline"]["tasks"]
ck("follow-ups due: tasks due today or the last 3 days, one per patient, quote patients first",
   [r["deal_id"] for r in TP["rows"]] == ["tq", "tn"] and TP["rows"][0]["when"] == "due today"
   and TP["rows"][1]["when"] == "due 2 days ago", [(r["deal_id"], r["when"]) for r in TP["rows"]])
ck("...a task's own step is shown", TP["rows"][0]["sub"].startswith("Quote Follow Up 3"), TP["rows"][0]["sub"])

# ---- Up Next: a promised time first, then a fresh lead and a text, then chance of closing
def _r(key, did, start_min, when="x"):
    return {"key": key, "deal_id": did, "start": (NOW + timedelta(minutes=start_min)).isoformat(), "when": when, "short": key}
UP = M.up_next({"late": [_r("late1", "a", -40)],
                "today": [_r("due", "b", 3, "9:44 AM"), _r("later", "c", 90, "11:11 AM")],
                "new_leads": [_r("fresh", "d", -6), _r("old", "e", -300)],
                "pipeline": {"texts": {"rows": [_r("tx", "f", -20)]}, "invoices": {"rows": [_r("inv", "g", -4000), _r("dup", "a", -4000)]},
                             "quotes": {"rows": [_r("q", "h", -9000)]}, "no_step": {"rows": [_r("ns", "i", -9000)]}}},
               NOW, show=9)
ck("up next: late, due now, fresh lead, texted back, invoice, quote, older lead, no next step",
   [r["key"] for r in UP["rows"]] == ["uplate1", "updue", "upfresh", "uptx", "upinv", "upq", "upold", "upns"], [r["key"] for r in UP["rows"]])
ck("...one row per patient (the invoice for the late patient is not repeated)", "updup" not in [r["key"] for r in UP["rows"]], "dup")
ck("...and it names the next booked call after the due ones", UP["next_booked"] == {"when": "11:11 AM", "short": "later"}, UP["next_booked"])
ck("...three on the page by default", len(M.up_next({"late": [_r(str(i), str(i), -30) for i in range(5)]}, NOW)["rows"]) == 3, "3")
ck("the built page carries up_next", isinstance(P.get("up_next"), dict) and P["up_next"]["rows"], P.get("up_next"))
# ---- the plan's measures on the page
PL = P.get("plan") or {}
ck("plan scorecard: results written match the numbers row", PL.get("results", {}).get("n") == P["nums"]["results"]
   and PL["results"]["of"] == P["nums"]["done"] and PL["results"]["goal"] == 90, PL.get("results"))
ck("...talked-nothing-booked is out of the calls where she talked", PL["talked_none"]["n"] == P["nums"]["talked_nothing_booked"]
   and PL["talked_none"]["of"] >= PL["talked_none"]["n"], PL["talked_none"])
ck("...late matches, goal 0", PL["late"]["n"] == P["nums"]["late"] and PL["late"]["goal"] == 0, PL["late"])
ck("...every measure says whether the day is on plan", all("ok" in v for v in PL.values()), list(PL))
K = M.build(dict(WORLD, calls_open=[], calls_today=[
        call("bk1", "d1", "Call scheduled with Shante", -120, "Completed"),
        call("dl1", "d1", "Outgoing call to Shante", -115, "Completed", "Reached", 200),
        call("bk2", "d2", "Call scheduled with Carmen", -60, "Overdue")], deals_new=[]),
    NOW, TZ, rep={"id": "u1", "name": "x"})["plan"]["promised"]
ck("promised times: one made 5 minutes after its time is kept, one still open is not", K["n"] == 1 and K["of"] == 2, K)
P_off = M.build(WORLD, NOW, TZ, rep={"id": "u1", "name": "Jessica Morales"}, queue_enabled=False)
ck("...and not at all when it is off", P_off["queue"] == [] and P_off["queue_enabled"] is False, P_off["queue"])

# ---- done, results, the red bar ----
nr = [r["call_id"] for r in P["no_result"]]
ck("a completed call with a blank or Pending result is 'no result yet'", sorted(nr) == ["done-noresult", "done-pending"], nr)
ck("a workflow's auto-completion is not a call done", P["nums"]["done"] == 7, P["nums"])
ck("results written = done minus no result", P["nums"]["results"] == 5, P["nums"])
labels = {d["label"]: d["n"] for d in P["done_summary"]}
ck("done today is grouped by call result", labels.get("Reached") == 3 and labels.get("No result yet") == 2, labels)

# ---- talked, nothing booked ----
ck("talked, nothing booked: >= 60 s, a reached result, and no open call after it on the deal",
   P["nums"]["talked_nothing_booked"] == 1, P["nums"])
ck("M.TALK_MIN_SECONDS is 60", M.TALK_MIN_SECONDS == 60, M.TALK_MIN_SECONDS)
ck("a dialer 'Disposition Timeout' is no result written", M.result_of({"Outgoing_call_disposition": "Disposition Timeout"}) == "",
   M.result_of({"Outgoing_call_disposition": "Disposition Timeout"}))
ck("a dialer disposition the agent picked is a result", M.result_of({"Call_Result": "Pending", "Outgoing_call_disposition": "Quoted"}) == "Quoted",
   "Quoted")
ck("reached by disposition when Call_Result is blank: 'Quoted' yes, 'No Answer' and 'Not Interested' no",
   M.is_reached({"Outgoing_call_disposition": "Quoted"}) and not M.is_reached({"Outgoing_call_disposition": "No Answer"})
   and not M.is_reached({"Outgoing_call_disposition": "Not Interested"}), "reach")

# ---- coordinator scaffold ----
PC = M.build(WORLD, NOW, TZ, role="coordinator", rep={"id": "u9", "name": "Jorge Mier"})
ck("coordinators get their own button set, not live yet",
   [o["key"] for o in PC["outcomes"]] == ["confirmed", "rescheduled", "paid", "call_back", "no_answer"]
   and PC["outcomes_live"] is False, [o["key"] for o in PC["outcomes"]])
ck("sales gets the nine engine outcomes", [o["key"] for o in P["outcomes"]] == [
   "booked_call", "consult_booked", "quote_sent", "call_back", "not_interested", "wrong_number",
   "do_not_call", "no_answer", "left_voicemail"] and P["outcomes_live"] is True, P["outcomes"])
ck("the reasons for Not interested", [x["key"] for x in P["reasons"]] == ["price", "not_ready", "went_elsewhere", "other"],
   P["reasons"])


# ── Start consultation (2026-10-20) ────────────────────────────────────────
ck("Start consultation is the estimator URL of the Zoho deal button",
   M.consult_url("5212466000123") == "https://estimator.goalsplasticsurgery.com?dealid=5212466000123", M.consult_url("5212466000123"))
ck("...and nothing without a real deal id", M.consult_url("") == "" and M.consult_url("d1") == "", M.consult_url("d1"))
ck("every row carries its consult link (empty when the row has no deal id)",
   all("consult" in r for r in P["late"] + P["today"] + P["new_leads"] + P["queue"] + P["no_result"]), "consult key")

# ── one conversation, one result (2026-10-20) ──────────────────────────────
def pdeal(did, name, created_min=-9 * 1440, stage="Call Scheduled", **kw):
    d = deal(did, name, created_min, stage=stage)
    d.update(kw)
    return d


PD = {d["id"]: d for d in [pdeal("2001", "Ana Booked"), pdeal("2002", "Bea Logged"), pdeal("2003", "Cora Neither"),
                           pdeal("2004", "Dee Auto"), pdeal("2005", "Eve Later")]}
PCALLS = [
    # A: the engine completed the booked call with the result; the dial has none
    call("a-b", "2001", "Follow-up: Ana", -60, "Completed", "Reached", 0),
    call("a-l", "2001", "Outgoing call to Ana Booked", -55, "Completed", None, 240),
    # B: the rep wrote the result on the dial in Zoho; the booked call is still open
    call("b-b", "2002", "Follow-up: Bea", -120, "Overdue"),
    call("b-l", "2002", "Outgoing call to Bea Logged", -110, "Completed", "Left Voicemail", 40),
    # C: neither has a result (the dial filed on the contact only, as a quarter are)
    call("c-b", "2003", "Follow-up: Cora", -200, "Overdue"),
    dict(call("c-l", None, "Outbound Call to Cora Neither", -190, "Completed", None, 90), Who_Id={"id": "c2003"}),
    # D: a workflow auto-completed the booked call (the rep wrote the result there);
    #    the dial is filed on the contact only
    call("d-b", "2004", "Follow-up Call", -300, "Completed", "Requested call back", 0, desc="Auto-completed by workflow"),
    dict(call("d-l", None, "Outgoing call to Dee Auto", -295, "Completed", None, 130), Who_Id={"id": "c2004"}),
    # E: a booked call due 35 minutes after the dial is a new attempt, not the same conversation
    call("e-l", "2005", "Outgoing call to Eve Later", -30, "Completed", None, 20),
    call("e-b", "2005", "Call scheduled with Eve", 5),
]
PW = {"calls_open": [c for c in PCALLS if c["Outgoing_Call_Status"] in ("Overdue", "Scheduled")],
      "calls_today": PCALLS, "deals": PD, "deals_new": [], "deal_calls": [], "contacts": {}}
PP = M.build(PW, NOW, TZ, rep={"id": "u1", "name": "Jessica Morales"})
nr_p = sorted(r["call_id"] for r in PP["no_result"])
lt_p = [r["call_id"] for r in PP["late"] + PP["today"]]
ck("booked + dial, result on the booked call: no red row", "a-l" not in nr_p and "a-b" not in nr_p, nr_p)
ck("booked + dial, result on the dial: no red row, and the booked call is not Late", "b-l" not in nr_p and "b-b" not in lt_p,
   (nr_p, lt_p))
ck("booked + dial, no result on either: ONE red row (the dial), not two, and not Late too",
   nr_p.count("c-l") == 1 and "c-b" not in nr_p and "c-b" not in lt_p, (nr_p, lt_p))
ck("...and that red row is on Cora's deal (found through her contact), so its Result button works",
   [r["deal_id"] for r in PP["no_result"] if r["call_id"] == "c-l"] == ["2003"], [(r["call_id"], r["deal_id"]) for r in PP["no_result"]])
ck("a booked call a workflow auto-completed does not strand its dial (paired through the contact)", "d-l" not in nr_p, nr_p)
ck("a booked call due well after the dial is the next attempt: still Today, the dial stays red",
   "e-b" in lt_p and "e-l" in nr_p, (nr_p, lt_p))
ck("the red bar counts conversations: C and E", nr_p == ["c-l", "e-l"], nr_p)
ck("done counts each conversation once (5 conversations, not 7 records)", PP["nums"]["done"] == 5, PP["nums"])
ck("results written counts conversations with a result on either record", PP["nums"]["results"] == 3, PP["nums"])
lab_p = {d["label"]: d["n"] for d in PP["done_summary"]}
ck("done is grouped by the conversation's result", lab_p.get("Reached") == 1 and lab_p.get("Left Voicemail") == 1
   and lab_p.get("Requested call back") == 1 and lab_p.get("No result yet") == 2, lab_p)

# ── the dialer tag (RingCX) ────────────────────────────────────────────────
DW = {"calls_open": [], "deals": {"3001": pdeal("3001", "Dial Timeout"), "3002": pdeal("3002", "Dial Picked")}, "deals_new": [],
      "deal_calls": [], "contacts": {},
      "calls_today": [dict(call("x-t", "3001", "Outbound Call to Dial Timeout", -40, "Completed", None, 50),
                           Outgoing_call_disposition="Disposition Timeout"),
                      dict(call("x-q", "3002", "Outbound Call to Dial Picked", -30, "Completed", "Pending", 300),
                           Outgoing_call_disposition="Quoted"),
                      call("x-x", "3001", "Outgoing call to Dial Timeout", -20, "Completed", None, 15)]}
DP = M.build(DW, NOW, TZ, rep={"id": "u1", "name": "Jessica Morales"})
dn = {r["call_id"]: r for r in DP["no_result"]}
ck("a dialer call is in Done; 'Disposition Timeout' lands in No result yet", "x-t" in dn, sorted(dn))
ck("...with the dialer tag; a RingEX 'Outgoing call' row has none", dn.get("x-t", {}).get("dialer") is True
   and dn.get("x-x", {}).get("dialer") is False, {k: v.get("dialer") for k, v in dn.items()})
ck("a disposition the agent picked is the result (counted, not red)", "x-q" not in dn
   and {d["label"]: d["n"] for d in DP["done_summary"]}.get("Quoted") == 1, DP["done_summary"])
ck("M.is_dialer reads the subject or a disposition", M.is_dialer({"Subject": "Outbound Call to X"})
   and M.is_dialer({"Subject": "x", "DialerDisposition": "No Answer"}) and not M.is_dialer({"Subject": "Outgoing call to X"}),
   "is_dialer")

# ── the pipeline (sales, the rep's own deals; 30-day windows, Danny 2026-10-20) ──
OWN = {"id": "u1", "name": "Rep"}
D = 1440
RIS = "Retainer Invoice Sent"
PIPE = [
    pdeal("1001", "Pat Contacted", -10 * D, Owner=OWN),
    pdeal("1002", "Quinn Quoted", -20 * D, stage="Quote Sent", Quote_Sent_Date=at(-2 * D), Owner=OWN),
    pdeal("1003", "Old Deal", -100 * D, stage="New Deal", Owner=OWN),
    pdeal("1004", "Lost Deal", -5 * D, stage="Closed Lost", Owner=OWN),
    pdeal("1005", "Nora Never", -3 * D, Owner=OWN),
    pdeal("1006", "Old Quote", -10 * D, stage="Quote Sent", Quote_Sent_Date=at(-120 * D), Owner=OWN),
    pdeal("1007", "Ivy Invoiced", -12 * D, stage=RIS, Retainer_Sent_Date=at(-4 * D), Retainer_Paid=False, Owner=OWN),
    pdeal("1008", "Paid Retainer", -12 * D, stage=RIS, Retainer_Sent_Date=at(-1 * D), Retainer_Paid=True, Owner=OWN),
    dict(pdeal("1009", "Unknown", -1 * D, Owner=OWN), Deal_Name="Missed Call - 5550100999",
         Contact_Name={"id": "c1009", "name": "Unknown"}),
    pdeal("1010", "Quinn Two", -6 * D, stage="Quote Sent", Quote_Sent_Date=at(-6 * D), Owner=OWN),
    pdeal("1012", "Edge In", -29 * D, stage="New Deal", Owner=OWN),                       # 29 days: in
    pdeal("1013", "Edge Out", -31 * D, stage="New Deal", Owner=OWN),                      # 31 days: out
    pdeal("1014", "Quote Edge In", -40 * D, stage="Quote Sent", Quote_Sent_Date=at(-29 * D), Owner=OWN),
    pdeal("1015", "Quote Edge Out", -40 * D, stage="Quote Sent", Quote_Sent_Date=at(-31 * D), Owner=OWN),
    pdeal("1016", "Inv Edge In", -40 * D, stage=RIS, Retainer_Sent_Date=at(-29 * D), Owner=OWN),
    pdeal("1017", "Inv Edge Out", -40 * D, stage=RIS, Retainer_Sent_Date=at(-31 * D), Owner=OWN),
    pdeal("1018", "Inv No Date", -5 * D, stage=RIS, Owner=OWN),                            # no date, new deal: in, last
    pdeal("1019", "Inv No Date Old", -31 * D, stage=RIS, Owner=OWN),                       # no date, old deal: out
    pdeal("1020", "Tess Texted", -8 * D, Owner=OWN),                                       # texted back: only there
    pdeal("1022", "Vera Invoiced", -9 * D, stage=RIS, Retainer_Sent_Date=at(-3 * D), Owner=OWN),   # invoiced, texted back
]
FUT = [call("f1", "1002", "Quote Follow Up 2", 24 * 60, "Scheduled"),
       call("f2", "1001", "Follow-up: Pat", -90, "Overdue")]          # past: not a next step
RC = [call("r1", "1001", "Outgoing call to Pat", -5 * D, "Completed", "Reached", 300)]
REPLIES = [{"phone": "5550190042", "dealId": "1020", "patientName": "Tess Texted", "inboundAt": at(-12),
            "lastText": "is 3pm ok?", "textCount": 2, "status": "sent"},
           {"phone": "5550190043", "dealId": "1010", "patientName": "Quinn Two", "inboundAt": at(-40),
            "lastText": "", "textCount": 1, "status": "sent"},
           {"phone": "5550190044", "dealId": "1022", "patientName": "Vera Invoiced", "inboundAt": at(-60),
            "lastText": "", "textCount": 1, "status": "held"}]
QW = dict(WORLD, pipe_deals=PIPE, future_open=FUT, rep_calls=RC, replies=REPLIES)
PQ = M.build(QW, NOW, TZ, rep={"id": "u1", "name": "Jessica Morales"})
pl = PQ["pipeline"]
ck("M.PIPE_DAYS is 30 (Danny's second answer)", M.PIPE_DAYS == 30, M.PIPE_DAYS)
ns = [r["deal_id"] for r in pl["no_step"]["rows"]]
ck("no next step: New Deal / Call Scheduled, created in 30 days, no open call from now on",
   set(ns) == {"1001", "1005", "1009", "1012"}, ns)
ck("...created 29 days ago is in, 31 days ago is out", "1012" in ns and "1013" not in ns, ns)
ck("...Quote Sent and Retainer Invoice Sent deals are not in it (their own lists)",
   not {"1002", "1006", "1007", "1010", "1014", "1016", "1018"} & set(ns), ns)
ck("...a closed stage is not listed", "1004" not in ns and "1003" not in ns, ns)
ck("...an overdue call in the past is not a next step", "1001" in ns, ns)
ck("...oldest last contact first: never called first (oldest deal first), then by last call",
   ns == ["1012", "1005", "1009", "1001"], ns)
ck("...the header count is the whole list", pl["no_step"]["n"] == 4, pl["no_step"]["n"])
ck("...on a rep's own page a nameless 'Missed Call' deal stays (the rep owns it)", "1009" in ns, ns)
QOWN = {"id": M.QUEUE_USER_IDS[0], "name": "Zoho Admin"}
PQQ = M.build(dict(QW, pipe_deals=[dict(d, Owner=QOWN) for d in PIPE]), NOW, TZ, rep=QOWN)
nsq = [r["deal_id"] for r in PQQ["pipeline"]["no_step"]["rows"]]
ck("...on a queue user's page it is left off (and the rest of that page's list stays)", "1009" not in nsq and "1005" in nsq, nsq)
qs = [r["deal_id"] for r in pl["quotes"]["rows"]]
ck("quotes waiting: Quote Sent, quoted in the last 30 days, newest quote first", qs == ["1002", "1014"], qs)
ck("...quoted 29 days ago is in, 31 days ago is out, 120 days ago is out", "1014" in qs and "1015" not in qs and "1006" not in qs, qs)
ck("...shows days since the quote and the next call", pl["quotes"]["rows"][0]["when"] == "2 days"
   and pl["quotes"]["rows"][0]["sub"] == "since quote" and pl["quotes"]["rows"][0]["next"].startswith("next call")
   and pl["quotes"]["rows"][1]["next"] == "no next call" and "no next call" in pl["quotes"]["rows"][1]["last"]["what"],
   [(r["when"], r["sub"], r.get("next")) for r in pl["quotes"]["rows"]])
iv = [r["deal_id"] for r in pl["invoices"]["rows"]]
ck("unpaid retainer invoices: Retainer Invoice Sent, Retainer_Paid not true, sent in 30 days, newest first, undated last",
   iv == ["1007", "1016", "1018"], iv)
ck("...sent 29 days ago is in, 31 days ago is out; undated counts by its created date",
   "1016" in iv and "1017" not in iv and "1018" in iv and "1019" not in iv, iv)
ck("...a paid retainer is not listed", "1008" not in iv, iv)
ck("...days since the invoice", pl["invoices"]["rows"][0]["when"] == "4 days"
   and pl["invoices"]["rows"][0]["sub"] == "since invoice", pl["invoices"]["rows"][0]["when"])
tx = pl["texts"]["rows"]
ck("texted back: closest to paying first (invoice 1022, quote 1010, then 1020), with the words when kept",
   [r["deal_id"] for r in tx] == ["1022", "1010", "1020"] and tx[2]["last"]["what"] == "“is 3pm ok?”"
   and tx[1]["last"]["what"] == "texted, words not kept", [(r["deal_id"], r["last"]) for r in tx])
ck("M.close_rank: invoice before quote before the rest",
   [M.close_rank(x) for x in ("Retainer Invoice Sent", "Quote Sent", "Call Scheduled", None)] == [0, 1, 2, 2], "rank")
ck("a patient who texted back is in that list only (not No next step, Quotes waiting or Unpaid invoices)",
   "1020" not in ns and "1010" not in qs and "1022" not in iv, (ns, qs, iv))
every = [r["deal_id"] for k in ("no_step", "quotes", "invoices", "texts") for r in pl[k]["rows"]]
ck("no deal appears in two pipeline lists", len(every) == len(set(every)), every)
ck("pipeline rows have Call, Result (a deal id) and Start consultation",
   all(r["deal_id"] and r["consult"] == M.CONSULT_URL % r["deal_id"] and r["phone"]
       for k in ("no_step", "quotes", "invoices", "texts") for r in pl[k]["rows"]), "rows")
cap_world = dict(QW, pipe_deals=[pdeal(str(5000 + i), "Lead %d" % i, -i * 60, Owner=OWN) for i in range(M.PIPE_SHOW + 7)])
pc = M.build(cap_world, NOW, TZ, rep={"id": "u1", "name": "x"})["pipeline"]["no_step"]
ck("capped like the other lists, with + N more", len(pc["rows"]) == M.PIPE_SHOW and pc["more"] == 7 and pc["n"] == M.PIPE_SHOW + 7,
   (len(pc["rows"]), pc["more"]))
PE = M.build(dict(WORLD, replies=None), NOW, TZ, rep={"id": "u1", "name": "x"})["pipeline"]
ck("texts that could not be loaded say so (never a false zero), and the rest still builds",
   PE["texts"].get("error") == "Couldn't load the texts just now." and PE["texts"]["rows"] == []
   and PE["no_step"].get("error"), PE["texts"])
ck("a deal with no call in the window reads 'never called'", {r["deal_id"]: r["when"] for r in pl["no_step"]["rows"]}.get("1005")
   == "never called", [(r["deal_id"], r["when"]) for r in pl["no_step"]["rows"]])
_pd = M.PIPE_DAYS
try:
    M.PIPE_DAYS = 90          # should the window ever be wider than the 30 days of calls read
    wide = M.build(dict(QW, pipe_deals=[pdeal("1021", "Older", -60 * D, Owner=OWN)], replies=[]), NOW, TZ,
                   rep={"id": "u1", "name": "x"})["pipeline"]["no_step"]["rows"]
finally:
    M.PIPE_DAYS = _pd
ck("...and a deal older than the calls read says '30+ days ago', not 'never called'",
   [r["when"] for r in wide] == ["30+ days ago"], [r["when"] for r in wide])
coql_deal = dict(pdeal("1011", "x", -2 * D, Owner=OWN), **{"Contact_Name": {"id": "c1011"},
                 "Contact_Name.First_Name": "Rosa", "Contact_Name.Last_Name": "Medina"})
nm = M.build(dict(QW, pipe_deals=[coql_deal]), NOW, TZ, rep={"id": "u1", "name": "x"})["pipeline"]["no_step"]["rows"]
ck("the name comes from Contact_Name.First_Name/Last_Name (COQL gives the lookup as an id only)",
   nm and nm[0]["short"] == "Rosa M." and nm[0]["full"] == "Rosa Medina", nm and nm[0]["full"])
# ── the 2026-09-24 bulk stamp is not a send date (Danny, 2026-10-20) ──────
BULK = [
    pdeal("1101", "Bulk Quote", -40 * D, stage="Quote Sent", Quote_Sent_Date="2026-09-24T11:30:00-04:00", Owner=OWN),
    pdeal("1102", "Bulk Quote Pm", -40 * D, stage="Quote Sent", Quote_Sent_Date="2026-09-24T13:19:00-04:00", Owner=OWN),
    pdeal("1103", "Real Quote Same Day", -40 * D, stage="Quote Sent", Quote_Sent_Date="2026-09-24T15:02:00-04:00", Owner=OWN),
    pdeal("1104", "Real Quote 23rd", -40 * D, stage="Quote Sent", Quote_Sent_Date="2026-09-23T11:30:00-04:00", Owner=OWN),
    pdeal("1105", "Real Quote 25th", -40 * D, stage="Quote Sent", Quote_Sent_Date="2026-09-25T11:30:00-04:00", Owner=OWN),
    pdeal("1106", "Bulk Invoice", -5 * D, stage=RIS, Retainer_Sent_Date="2026-09-24T13:20:00-04:00", Owner=OWN),
    pdeal("1107", "Real Invoice 25th", -40 * D, stage=RIS, Retainer_Sent_Date="2026-09-25T13:20:00-04:00", Owner=OWN),
    pdeal("1108", "Real Invoice 23rd", -40 * D, stage=RIS, Retainer_Sent_Date="2026-09-23T13:20:00-04:00", Owner=OWN),
]
BP = M.build(dict(QW, pipe_deals=BULK, replies=[]), NOW, TZ, rep={"id": "u1", "name": "x"})["pipeline"]
bq = [r["deal_id"] for r in BP["quotes"]["rows"]]
bi = [r["deal_id"] for r in BP["invoices"]["rows"]]
bn = [r["deal_id"] for r in BP["no_step"]["rows"]]
ck("a Quote_Sent_Date in a 2026-09-24 bulk batch (11:30, 13:19) is not a quote waiting", "1101" not in bq and "1102" not in bq, bq)
ck("...a real quote that day outside the batches, or on 09-23 or 09-25, is kept",
   bq == ["1105", "1103", "1104"], bq)
ck("a Retainer_Sent_Date in the 13:18-13:21 batch is not an unpaid invoice (and not counted by its created date)",
   "1106" not in bi, bi)
ck("...a real invoice on 09-23 or 09-25 is kept", bi == ["1107", "1108"], bi)
ck("...and the bulk-stamped deals do not move into No next step", not {"1101", "1102", "1106"} & set(bn), bn)
ck("is_bulk_stamp: the windows are per field and to the minute, Eastern",
   M.is_bulk_stamp("Quote_Sent_Date", "2026-09-24T15:17:00Z") and not M.is_bulk_stamp("Quote_Sent_Date", "2026-09-24T15:42:00Z")
   and M.is_bulk_stamp("Retainer_Sent_Date", "2026-09-24T17:21:00Z") and not M.is_bulk_stamp("Retainer_Sent_Date", "2026-09-24T15:30:00Z")
   and not M.is_bulk_stamp("Quote_Sent_Date", None), "is_bulk_stamp")
ck("coordinators get no pipeline (sales role only)", PC["pipeline"] == {"enabled": False}, PC["pipeline"])

# ---- the time picker ----
busy = [{"Call_Start_Time": at(79), "Call_Duration_in_seconds": 600}]
slots = M.free_slots(busy, NOW, TZ)
starts = [datetime.fromisoformat(s["at"]) for s in slots]
ck("no open slot sooner than 15 minutes from now", min(starts) >= NOW + timedelta(minutes=15), min(starts))
ck("a booked call takes its slot", all(not (s <= NOW + timedelta(minutes=79) < s + timedelta(minutes=30)) for s in starts),
   [s.isoformat() for s in starts][:6])
ck("no Sunday slots", all(s.astimezone(TZ).weekday() != 6 for s in starts), "sunday")
txt = M.confirmation_preview("Carmen", "Jessica", NOW + timedelta(days=1), TZ)
ck("the confirmation text: names both, no hyphen or dash (patient text voice)",
   "Carmen" in txt and "Jessica" in txt and "-" not in txt and "—" not in txt and "–" not in txt, txt)

# ── the app: gate, identity, proxy ─────────────────────────────────────────
os.environ.setdefault("MY_DAY_FAKE", "")
import app as A  # noqa: E402

c = A.app.test_client()
saved = (A.GOOGLE_CLIENT_ID, A.MY_DAY_FAKE, A.BACKOFFICE_URL, A.OVERDUE_API_KEY, A.MY_DAY_MANAGERS,
         A._zoho.list_users_detail, A._zoho.my_day_world, A.requests.post, A._load_ringex_snapshot,
         A._snapshot_ringex, A._zoho.my_day_pipeline, A.requests.get)
USERS = [{"id": "u1", "name": "Jessica Morales", "email": "jess@goalsplasticsurgery.com"},
         {"id": "u2", "name": "Mabel Alvarez", "email": "mabel@goalsplasticsurgery.com"}]
sent = []


class _R:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def fake_post(url, json=None, headers=None, **k):
    sent.append({"url": url, "json": json, "headers": headers or {}})
    if url.endswith("/api/call-outcome") and json and json.get("outcome") == "quote_sent":
        return _R(404, {"error": "not found"})
    if url.endswith("/api/call-outcome"):
        return _R(200, {"ok": True, "saved": True, "next": "Call back Thursday", "undoToken": "tok1"})
    if url.endswith("/api/call-outcome/undo"):
        return _R(200, {"ok": True})
    if url.endswith("/api/lead-claim"):
        return _R(409, {"claimedBy": "Mabel"})
    return _R(404, {})


gets = []


def fake_get(url, params=None, headers=None, **k):
    gets.append({"url": url, "params": params, "headers": headers or {}})
    if url.endswith("/api/reply-pings/open") and GET_MODE["ok"]:
        return _R(200, {"rep": params.get("rep"), "hours": 48, "open": REPLIES[:1]})
    return _R(500, {"error": "down"})


GET_MODE = {"ok": True}


def ringex_forbidden(*a, **k):
    raise AssertionError("My Day must not read RingEX")


try:
    A.GOOGLE_CLIENT_ID = "x"
    A.MY_DAY_FAKE = False
    A.BACKOFFICE_URL = "https://backoffice.example.test"
    A.OVERDUE_API_KEY = "SECRET-KEY-123"
    A.MY_DAY_MANAGERS = {"boss@goalsplasticsurgery.com"}
    A._md_users.update(at=0.0, rows=None)
    A._md_cache.clear()
    A._zoho.list_users_detail = lambda: USERS
    A._zoho.my_day_world = lambda *a, **k: WORLD
    A._zoho.my_day_pipeline = lambda *a, **k: {"pipe_deals": PIPE, "future_open": FUT, "rep_calls": RC}
    A.requests.get = fake_get
    A._load_ringex_snapshot = ringex_forbidden
    A._snapshot_ringex = ringex_forbidden
    A.requests.post = fake_post

    r = c.get("/my-day")
    ck("/my-day is closed without the sales board's door (login redirect or the word page)",
       r.status_code == 302 or (b"password" in r.data.lower() and b"BOOT" not in r.data), r.status_code)
    ck("/my-day/api/data is closed too", c.get("/my-day/api/data").status_code == 401, "401")

    with c.session_transaction() as s:
        s["user"] = {"email": "jess@goalsplasticsurgery.com", "name": "Jessica Morales"}
    r = c.get("/my-day")
    html = r.get_data(as_text=True)
    ck("a signed-in rep gets her own page, matched to Zoho by email", r.status_code == 200 and '"id": "u1"' in html, r.status_code)
    ck("the back-office key is never in the page", "SECRET-KEY-123" not in html, "key in html")
    d = c.get("/my-day/api/data").get_json()
    want = M.build(WORLD, datetime.now(timezone.utc), A.MY_DAY_TZ, rep={"id": "u1", "name": "x"})["nums"]
    ck("her data is her own calls, built from Zoho with no RingEX read",
       d.get("rep", {}).get("id") == "u1" and d.get("nums") == want, (d.get("error"), d.get("nums"), want))
    ck("a rep cannot open someone else's page", c.get("/my-day/api/data?rep=u2").status_code == 403, "403")
    g = gets[-1] if gets else {}
    ck("texted back is read from the back office with the shared key, for this rep, 48 h",
       g.get("url") == "https://backoffice.example.test/api/reply-pings/open" and g["params"] == {"rep": "u1", "hours": 48}
       and g["headers"].get("x-api-key") == "SECRET-KEY-123", g)
    ck("...and its rows reach the page", [r["deal_id"] for r in d["pipeline"]["texts"]["rows"]] == ["1020"],
       d.get("pipeline", {}).get("texts"))
    GET_MODE["ok"] = False
    A._md_cache.clear()
    d2 = c.get("/my-day/api/data").get_json()
    ck("the back office down: the page still loads, the texts list says it couldn't load",
       d2.get("nums") == want and d2["pipeline"]["texts"].get("error") == "Couldn't load the texts just now."
       and d2["pipeline"]["no_step"]["n"] == 5, d2.get("pipeline", {}).get("texts"))
    GET_MODE["ok"] = True

    sent.clear()
    r = c.post("/my-day/api/outcome", json={"rep": "u2", "dealId": "123", "outcome": "no_answer"})
    ck("...or write a result as someone else", r.status_code == 403 and not sent, (r.status_code, sent))
    r = c.post("/my-day/api/outcome", json={"dealId": "123", "callId": "9", "outcome": "no_answer"})
    j = r.get_json()
    ck("a result goes to the back office with the shared key in x-api-key",
       j.get("ok") and sent and sent[-1]["url"] == "https://backoffice.example.test/api/call-outcome"
       and sent[-1]["headers"].get("x-api-key") == "SECRET-KEY-123", (j, sent[-1:] and sent[-1]["url"]))
    ck("...with the rep's own Zoho id, the deal and the call",
       sent[-1]["json"] == {"dealId": "123", "callId": "9", "outcome": "no_answer", "repZohoId": "u1"}, sent[-1]["json"])
    ck("...and the answer carries next + undo, never the key",
       j.get("undoToken") == "tok1" and j.get("next") == "Call back Thursday" and "SECRET" not in r.get_data(as_text=True), j)
    r = c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "quote_sent"})
    ck("a 404 from the engine (not deployed yet) says 'Results are coming soon', not an error",
       r.status_code == 200 and r.get_json().get("coming_soon") is True
       and r.get_json().get("message") == "Results are coming soon.", r.get_json())
    n0 = len(sent)
    r = c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "do_not_call"})
    ck("Do not call needs confirm: true", r.status_code == 400 and len(sent) == n0, r.get_json())
    c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "do_not_call", "confirm": True})
    ck("...and passes it on", sent[-1]["json"].get("confirm") is True, sent[-1]["json"])
    r = c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "not_interested"})
    ck("Not interested asks why", r.status_code == 400 and r.get_json().get("error") == "reason_required", r.get_json())
    c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "not_interested", "reason": "price"})
    ck("...and sends the reason", sent[-1]["json"].get("reason") == "price", sent[-1]["json"])
    r = c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "booked_call"})
    ck("Booked a call needs a time", r.status_code == 400, r.get_json())
    c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "booked_call", "at": "2026-10-21T15:00",
                                        "sendConfirmation": True})
    ck("...a picked local time goes as UTC ISO with the Send choice",
       sent[-1]["json"].get("at") == "2026-10-21T19:00:00Z" and sent[-1]["json"].get("sendConfirmation") is True,
       sent[-1]["json"])
    r = c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "delete_everything"})
    ck("an outcome the engine does not know is refused here", r.status_code == 400, r.status_code)
    r = c.post("/my-day/api/outcome", data="dealId=123&outcome=no_answer",
               content_type="application/x-www-form-urlencoded")
    ck("a form post (cross-site) is refused: JSON only", r.status_code == 415, r.status_code)
    r = c.post("/my-day/api/outcome/undo", json={"undoToken": "tok1"})
    ck("undo forwards the token", r.get_json().get("ok") and sent[-1]["json"] == {"undoToken": "tok1"}, sent[-1])
    import time as _t
    A._md_holds.clear()
    A._md_holds["555"] = {"rep_id": "u2", "name": "Mabel Alvarez", "at": _t.time()}
    n_posts = len(sent)
    r = c.post("/my-day/api/claim", json={"dealId": "555"})
    ck("an unclaimed lead another rep is calling now stops the call, naming her", r.get_json().get("go") is False
       and "Mabel" in r.get_json().get("message", ""), r.get_json())
    r = c.post("/my-day/api/claim", json={"dealId": "556"})
    ck("...a free one goes through and is held for this rep, with nothing sent to the back office",
       r.get_json().get("go") is True and A._md_holds["556"]["rep_id"] == "u1" and len(sent) == n_posts, r.get_json())
    A._md_holds["555"]["at"] = _t.time() - 16 * 60
    ck("...a hold lasts 15 minutes", c.post("/my-day/api/claim", json={"dealId": "555"}).get_json().get("go") is True, "expired")
    A._md_holds.clear()
    A._md_holds["q1"] = {"rep_id": "u2", "name": "Mabel Alvarez", "at": _t.time()}
    A._md_cache.clear()
    _qo = A.MY_DAY_QUEUE_OWNER_IDS
    A.MY_DAY_QUEUE_OWNER_IDS = ["5212466000000357001"]
    q = c.get("/my-day/api/data").get_json().get("queue") or []
    A.MY_DAY_QUEUE_OWNER_IDS = _qo
    A._md_cache.clear()
    ck("...and the unclaimed list shows who is calling", any(x.get("deal_id") == "q1" and x.get("held_by") == "Mabel Alvarez"
       and x.get("held_mine") is False for x in q), q)
    A._md_holds.clear()

    # the floor password reads, never writes
    with c.session_transaction() as s:
        s.pop("user", None)
        s["v5_pw"] = True
    sent.clear()
    r = c.post("/my-day/api/outcome", json={"dealId": "123", "outcome": "no_answer"})
    ck("the shared floor word cannot write a result", r.status_code == 403 and not sent, r.status_code)
    ck("...but can read a page it picks", c.get("/my-day/api/data?rep=u2").status_code == 200, "read")

    # a manager opens anyone's page
    with c.session_transaction() as s:
        s.pop("v5_pw", None)
        s["user"] = {"email": "boss@goalsplasticsurgery.com", "name": "Boss"}
    A._md_cache.clear()
    r = c.get("/my-day/u2")
    ck("a manager opens any rep's page, with the name switch", r.status_code == 200 and "Mabel Alvarez" in r.get_data(as_text=True),
       r.status_code)
    ck("...and the data is that rep's", c.get("/my-day/api/data?rep=u2").get_json().get("rep", {}).get("id") == "u2", "u2")
    c.post("/my-day/api/outcome", json={"rep": "u2", "dealId": "123", "outcome": "no_answer"})
    ck("...and a result written there is filed under that rep", sent and sent[-1]["json"]["repZohoId"] == "u2", sent[-1:])

    # the admin password, typed in the My Day box, opens any rep's page and writes
    with c.session_transaction() as s:
        s.clear()
    A.MY_DAY_ADMIN_PASSWORDS = ("admin",)
    A._v5_pw_tries.clear()
    r = c.post("/my-day/u2", data={"password": "nope"})
    ck("a wrong password at My Day stays on the door and says so",
       r.status_code == 401 and b"not the password" in r.data, r.status_code)
    r = c.post("/my-day/u2", data={"password": "admin"})
    ck("the admin password at My Day comes back to the same page", r.status_code == 302
       and r.headers.get("Location", "").endswith("/my-day/u2"), (r.status_code, r.headers.get("Location")))
    with c.session_transaction() as s:
        ck("...as the My Day admin, not a signed-in user of the whole app",
           s.get("md_admin") is True and "user" not in s and not s.get("v5_pw"), dict(s))
    A._md_cache.clear()
    r = c.get("/my-day/u2")
    ck("...which opens any rep's page with the name switch", r.status_code == 200 and "Mabel Alvarez" in r.get_data(as_text=True)
       and '"manager": true' in r.get_data(as_text=True), r.status_code)
    sent.clear()
    c.post("/my-day/api/outcome", json={"rep": "u2", "dealId": "123", "outcome": "no_answer"})
    ck("...and can write a result, filed under that rep", sent and sent[-1]["json"]["repZohoId"] == "u2", sent[-1:])
    ck("...but does not open the rest of the app", c.get("/v6").status_code != 200 or b"password" in c.get("/v6").data.lower(),
       c.get("/v6").status_code)
    A.MY_DAY_ADMIN_PASSWORDS = ()
    A._v5_pw_tries.clear()
    with c.session_transaction() as s:
        s.clear()
        s["user"] = {"email": "boss@goalsplasticsurgery.com", "name": "Boss"}

    # the floor view: managers only, sales reps listed, each linking to their day
    r = c.get("/my-day/floor")
    ck("a manager opens the floor view, sales reps listed with links to their day",
       r.status_code == 200 and "/my-day/u2" in r.get_data(as_text=True) or '"id": "u2"' in r.get_data(as_text=True), r.status_code)
    with c.session_transaction() as s2:
        _saved_user = s2.get("user"); s2["user"] = {"email": "jess@goalsplasticsurgery.com", "name": "Jessica Morales"}
    ck("...a rep is sent back to their own day", c.get("/my-day/floor").status_code == 302, "302")
    with c.session_transaction() as s2:
        s2["user"] = _saved_user

    # live phone time comes from the sales board's report, cut to the rep
    _fr = A._md_floor_report
    A._md_floor_report = lambda: {"ranked": [{"name": "Mabel Alvarez", "talk": 3600, "wrap": 300, "attempts": 40, "connected": 9},
                                             {"name": "Other Rep", "talk": 1200, "wrap": 0, "attempts": 10, "connected": 2},
                                             {"name": "Third Rep", "talk": 2400, "wrap": 0, "attempts": 20, "connected": 4}],
                                  "meta": {"covers_to": "09:41:00"}}
    j = c.get("/my-day/api/handle?rep=u2").get_json()
    ck("phone time: talk, handle = talk + wrap, calls, connected, floor middle", j.get("found") and j["talk"] == 3600
       and j["handle"] == 3900 and j["dials"] == 40 and j["connected"] == 9 and j["floor_median_talk"] == 2400, j)
    A._md_floor_report = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    ck("...the report down is a 502, not a crash", c.get("/my-day/api/handle?rep=u2").status_code == 502, "502")
    A._md_floor_report = _fr

    # unconfigured back office reads as coming soon, not a crash
    A.BACKOFFICE_URL = ""
    r = c.post("/my-day/api/outcome", json={"rep": "u2", "dealId": "123", "outcome": "no_answer"})
    ck("no BACKOFFICE_URL: 'Results are coming soon'", r.get_json().get("coming_soon") is True, r.get_json())
finally:
    (A.GOOGLE_CLIENT_ID, A.MY_DAY_FAKE, A.BACKOFFICE_URL, A.OVERDUE_API_KEY, A.MY_DAY_MANAGERS,
     A._zoho.list_users_detail, A._zoho.my_day_world, A.requests.post, A._load_ringex_snapshot,
     A._snapshot_ringex, A._zoho.my_day_pipeline, A.requests.get) = saved

src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
blk = src[src.index("# ── My Day (build plan step 21)"):src.index('@app.route("/v6/board")')]
import re as _re  # noqa: E402
_rc = _re.findall(r"_ringcx\b|_snapshot_ringex|_load_ringex_snapshot|_v6_fetch\w*|_fetch_supplemental\w*|"
                  r"_cx_\w+\(|ringcx_client|_presence\w*\(", blk)
ck("the My Day block calls no RingCentral reader (the RingEX budget is billing's)", _rc == [], _rc)

for e in errors:
    print("FAIL:", e)
print("checks_my_day: %d passed, %d failed" % (passed, len(errors)))
print("%d failed" % len(errors))
sys.exit(1 if errors else 0)
