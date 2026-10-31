"""Coordinator My Day (Danny, 2026-10-31): the coordinators' own /my-day.

Fails when a rule in coord_day.py, or a safety rule of the result writes in
app.py, is broken: P1 first, the contact limits, the four scorecard numbers,
the drafts' house style, a write only on the coordinator's own Journey call,
trigger [] completion with the exact Call_Result values, a call back booked at
the picked time, never a cancelled call, and the reps' page left as it was.
Run: ./.venv/bin/python -B checks_coord_day.py   (tally LAST)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coord_day as C  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 14, 0, tzinfo=TZ).astimezone(timezone.utc)      # a Monday, 2 PM
TODAY = NOW.astimezone(TZ).date()
ME = "5212466000531206001"
OTHER = "5212466000454324769"
SCHED = C.schedules_from_rows([
    {"Staff_User": {"id": ME}, "Name": "Jorge Mier", "Team": "Coordinator", "Active": True,
     "Start_Hour": 12, "End_Hour": 21, "Days_Off": ["Sat", "Sun"]},
    {"Staff_User": {"id": OTHER}, "Name": "Judith Merlo", "Team": "Coordinator", "Active": True,
     "Start_Hour": 10, "End_Hour": 19, "Days_Off": ["Sat", "Sun"]},
    {"Staff_User": {"id": "999"}, "Name": "Gone", "Team": "Coordinator", "Active": False},
])


def at(minutes):
    return (NOW + timedelta(minutes=minutes)).astimezone(TZ).isoformat()


def d(n):
    return (TODAY + timedelta(days=n)).isoformat()


def J(jid, out, bal=0, dl=None, lrc_days=None, owner=ME, stage="Surgery Preparation", created_days=-60, name="Pat Doe"):
    fn, ln = name.split()
    return {"id": jid, "Name": name, "Surgery_Date": d(out), "Journey_Stage": stage, "Case_Status": "Active",
            "Journey_Balance_Due": bal, "Fourth_Attempt_Date_15D": (d(dl) + "T09:00:00-05:00") if dl is not None else None,
            "Lab_Result_Status": "In Review", "Clearance_Status": None,
            "Next_Follow_Up_Date1": at(lrc_days * 1440) if lrc_days is not None else None,
            "Owner": {"id": owner}, "Created_Time": at(created_days * 1440), "Contact": {"id": "c" + jid},
            "Contact.First_Name": fn, "Contact.Last_Name": ln, "Contact.Mobile": "5550100" + "".join(ch for ch in jid if ch.isdigit()).rjust(3, "0")}


def K(cid, jid, subject, start_min, status="Scheduled", result=None, owner=ME, secs=None, ctype="Outbound", agenda=None):
    return {"id": cid, "Subject": subject, "Call_Type": ctype, "Outgoing_Call_Status": status,
            "Call_Start_Time": at(start_min), "Call_Result": result, "Call_Duration_in_seconds": secs,
            "Call_Agenda": agenda, "Description": "GOAL: x", "What_Id": {"id": jid}, "Owner": {"id": owner},
            "$se_module": "CustomModule13"}


# ── the subject ───────────────────────────────────────────────────────────
s = C.parse_subject("Carmen Vega · Pre-op confirm · Surg Nov 5 · P1")
ck("planner subject: purpose and P#", s["purpose"] == "Pre-op confirm" and s["prio"] == 1, s)
s = C.parse_subject("Carmen Vega · Attempt 2/2: check-in · Surg Dec 4 · P3")
ck("Zoho planner 'Attempt n/m:' is the attempt, the purpose is what follows", s["attempt"] == "2/2" and s["purpose"] == "Check-in" and s["prio"] == 3, s)
s = C.parse_subject("Carmen Vega · Check-in (retry) · Surg Dec 4 · P3")
ck("journey-retry's '(retry)' is a flag, not part of the purpose", s["retry"] and s["purpose"] == "Check-in", s)
ck("billing calls read as billing", C.parse_subject("Billing call - Overdue $1,200 - Carmen Vega")["purpose"].startswith("Billing call"), "")
ck("kinds", [C.kind_of(x) for x in ("Day-before confirm", "Pre-op confirm", "Post-op day 1", "Post-op week 1",
                                    "50% payment due Nov 5", "Labs + clearance due Nov 9", "Welcome", "Call back",
                                    "Check-in", "Confirm surgery happened")] ==
   ["day_before", "preop", "postop1", "postop", "payment", "labs", "welcome", "callback", "checkin", "happened"], "")
ck("a bare 'Name · P1' row takes its kind from the briefing", C.kind_of("", "P1 · Full balance due Oct 27 · surg") == "payment", "")

# ── priority ──────────────────────────────────────────────────────────────
ck("Danny's P1 list beats a lower P#: pre-op tagged P3 is P1", C.priority("preop", 3, None, 0) == 1, "")
ck("a payment heads-up stays its P# while the deadline is far", C.priority("payment", 2, 10, 4000) == 2, "")
ck("...and is P1 within 3 days of the deadline with money owed", C.priority("payment", 2, 3, 4000) == 1, "")
ck("...or after it passed", C.priority("payment", 2, -4, 4000) == 1, "")
ck("a check-in with no P# is P3, labs P2", C.priority("checkin", None, None, 0) == 3 and C.priority("labs", None, None, 0) == 2, "")

# ── contact limits ────────────────────────────────────────────────────────
ck("30 days at 90+ out, 14 at 31 to 90, 7 at 30 or less and after surgery",
   [C.contact_limit(x) for x in (120, 91, 90, 31, 30, 5, -10)] == [30, 30, 14, 14, 7, 7, 7], "")

# ── a day ─────────────────────────────────────────────────────────────────
JOURNEYS = [
    J("j1", 1, name="Carmen Vega"),                                   # day before, late
    J("j2", 9, bal=2400, dl=2, lrc_days=-12, name="Lauren Pierce"),   # payment, deadline in 2 days -> P1
    J("j3", 48, created_days=-5, name="Daniela Cruz"),                # welcome, never reached
    J("j4", 120, lrc_days=-20, name="Nicole Hart"),                   # check-in later today, within 30
    J("j5", 20, lrc_days=-2, owner=OTHER, name="Ana Reyes"),          # someone else's
    J("j6", -1, stage="Post-Op Care", lrc_days=-1, name="Tanya Brooks"),
    J("j7", 60, bal=900, dl=5, lrc_days=-3, name="Rosa Medina"),      # deadline in 5 days, talked this week -> covered
    J("j8", 40, bal=900, dl=6, lrc_days=-40, name="Erica Lane"),      # deadline in 6 days, a call booked before it -> covered
    J("j10", 70, lrc_days=-20, name="Jade Turner"),                   # 20 days quiet at 70 out, nothing booked
]
CALLS = [
    K("91001", "j1", "Carmen Vega · Day-before confirm · Surg Nov 3 · P3", -50, "Overdue"),
    K("91002", "j2", "Lauren Pierce · Full balance due Nov 4 · Surg Nov 11 · P2", -2),
    K("91003", "j3", "Daniela Cruz · Welcome · Surg Dec 20 · P2", -20, "Overdue"),
    K("91000", "j3", "Daniela Cruz · Attempt 1/2: welcome · Surg Dec 20 · P2", -10, "Overdue"),   # same Journey twice
    K("91004", "j4", "Nicole Hart · Check-in · Surg Mar 2 · P3", 90),
    K("91005", "j5", "Ana Reyes · Check-in · Surg Nov 22 · P3", -5, owner=OTHER),
    K("91006", "j8", "Erica Lane · 50% payment due Nov 8 · Surg Dec 12 · P2", 3 * 1440),
    K("91007", "j6", "Tanya Brooks · Post-op day 1 · Surg Nov 1 · P1", -180, "Completed", "Reached", secs=400),
    K("91008", "j3", "Daniela Cruz · Welcome · Surg Dec 20 · P2", -2 * 1440, "Completed", "No Answer / Not Reached", secs=20),
    K("91009", "j3", "Outgoing call to Daniela Cruz", -60, "Completed", None, secs=25),
    K("91010", "j4", "Nicole Hart · Check-in · Surg Mar 2 · P3", -100, "Completed", "Left Voicemail", secs=30),
    K("91011", "j4", "Nicole Hart · Check-in · Surg Mar 2 · P3", -30, "Cancelled"),
    dict(K("91012", "j4", "A deal call", -40), **{"$se_module": "Deals"}),
]
WORLD = {"journeys": JOURNEYS, "calls": CALLS}
P = C.build(WORLD, NOW, TZ, {"id": ME, "name": "Jorge Mier"}, SCHED)
rn = [(r["call_id"], r["prio"]) for r in P["right_now"]]
ck("Right now: P1 first (day-before tagged P3 and a payment due in 2 days are P1), then P2", rn[:2] == [("91001", 1), ("91002", 1)] and rn[2][1] == 2, rn)
ck("one row per Journey: the older open call for the same patient leads", [r["call_id"] for r in P["right_now"]].count("91003") == 1 and "91000" not in [r["call_id"] for r in P["right_now"]], rn)
ck("only her own calls, only Journey calls", all(r["call_id"] not in ("91005", "91012") for r in P["right_now"] + P["later"]), rn)
ck("a cancelled call is nowhere", all(r["call_id"] != "91011" for r in P["right_now"] + P["later"] + P["done"]), "")
ck("Later today: the 3:30 PM check-in", [r["call_id"] for r in P["later"]] == ["91004"], [r["call_id"] for r in P["later"]])
ck("Up next = the first three of Right now", [r["call_id"] for r in P["up_next"]] == [r["call_id"] for r in P["right_now"][:3]], P["up_next"])
ck("Done today: her completed calls today, newest first", [r["call_id"] for r in P["done"]] == ["91009", "91010", "91007"], [r["call_id"] for r in P["done"]])
r1 = P["right_now"][0]
ck("late label", r1["sub"] == "50 m late" and r1["sub_kind"] == "late", r1["sub"])
ck("the row: surgery tomorrow", r1["days_out"] == 1 and r1["days_out_label"] == "tomorrow", r1)
r2 = next(r for r in P["right_now"] if r["call_id"] == "91002")
ck("the row: balance and next deadline", r2["balance_label"] == "$2,400" and r2["deadline_days"] == 2 and r2["deadline_label"] == "in 2 days", r2)
r3 = next(r for r in P["right_now"] if r["call_id"] == "91003")
ck("the row: never reached, tries counted (a no-answer and a short logged dial)", r3["last_days"] is None and r3["tries"] == 2, (r3["last_label"], r3["tries"]))
ck("the row: last real contact from the Journey field", r2["last_days"] == 12, r2["last_label"])
ck("the row: Zoho Journey link and the phone for the Call button", r2["zoho"].endswith("/CustomModule13/j2") and len(r2["phone"]) == 10, r2["zoho"])
ck("nums", P["nums"]["right_now"] == 3 and P["nums"]["p1"] == 2 and P["nums"]["late"] == 2 and P["nums"]["done"] == 3 and P["nums"]["no_result"] == 1 and P["nums"]["reached"] == 1, P["nums"])
ck("her shift today", P["shift"] == "12 PM to 9 PM", P["shift"])

# ── drafts ────────────────────────────────────────────────────────────────
for kind in ("welcome", "day_before", "preop", "happened", "postop1", "postop", "payment", "labs", "callback", "checkin", "priority"):
    t = C.draft_text(kind, "Carmen", "Jorge", "Thursday, Nov 5", "Monday, Nov 2")
    lines = t.split("\n")
    ck("draft %s: ☺️, line breaks, short, no dashes, one question or ask" % kind,
       "☺️" in t and 3 <= len(lines) <= 4 and len(t) <= 260 and not any(ch in t for ch in "-–—")
       and (t.count("?") <= 1), t)
ck("every row carries both drafts and the 'Danny to approve' note", all(r["draft"] and r["draft_missed"] for r in P["right_now"]) and "Danny to approve" in P["draft_note"], "")

# ── scorecard ─────────────────────────────────────────────────────────────
S = P["score"]
# live mine: j1 (no talk, limit 7) j2 (12 d, limit 7) j3 (none) j4 (20 d, limit 30) j6 (1 d, 7) j7 (3 d, 14) j8 (40 d, 14)
ck("contact limit: j4, j6, j7 inside; j1, j2, j3, j8, j10 outside", (S["contact"]["n"], S["contact"]["of"]) == (3, 8), S["contact"])
qb = [q["booked"] for q in S["contact"]["quiet"]]
ck("the quiet list puts the ones with nothing booked first", qb == sorted(qb) and False in qb, qb)
ck("reach rate: 1 reached of 3 with a result in 7 days (the dial with no result is left out)", (S["reach"]["n"], S["reach"]["of"]) == (1, 3), S["reach"])
ck("deadlines: j2 (talk 12 d ago, booked call is today and counts), j7 (talk 3 d), j8 (call booked before) = 3 of 3",
   (S["deadlines"]["n"], S["deadlines"]["of"]) == (3, 3), S["deadlines"])
W2 = {"journeys": JOURNEYS, "calls": [c for c in CALLS if c["id"] not in ("91002", "91006")]}
S2 = C.build(W2, NOW, TZ, {"id": ME, "name": "Jorge Mier"}, SCHED)["score"]
ck("...without the booked calls, j2 and j8 are not covered", (S2["deadlines"]["n"], S2["deadlines"]["of"]) == (1, 3)
   and {m["journey_id"] for m in S2["deadlines"]["missing"]} == {"j2", "j8"}, S2["deadlines"])
# welcomes: j3 created 5 days ago (Wednesday), next working day Thursday, never reached -> late
ck("welcomes: j3 booked 5 days ago, not reached by the next working day", (S["welcomes"]["n"], S["welcomes"]["of"]) == (0, 1)
   and S["welcomes"]["late"][0]["journey_id"] == "j3", S["welcomes"])
W3 = {"journeys": JOURNEYS, "calls": CALLS + [K("91013", "j3", "Daniela Cruz · Welcome · Surg Dec 20 · P2", -4 * 1440, "Completed", "Reached", secs=300)]}
ck("...a talk the next day counts", C.build(W3, NOW, TZ, {"id": ME, "name": "x"}, SCHED)["score"]["welcomes"]["n"] == 1, "")
# a Journey created Friday evening: the window runs to the end of Monday for a Mon-Fri coordinator
fri = J("j9", 30, created_days=-2.9, name="Fri Day")
S4 = C.build({"journeys": JOURNEYS + [fri], "calls": CALLS}, NOW, TZ, {"id": ME, "name": "x"}, SCHED)["score"]["welcomes"]
ck("a Friday booking's window is still open on Monday afternoon (working days, not calendar days)", S4["open"] == 1, S4)

# ── schedules ─────────────────────────────────────────────────────────────
ck("inactive Team_Schedules rows are not coordinators", {s["id"] for s in SCHED} == {ME, OTHER}, SCHED)
ck("no rows -> the fallback six", len(C.schedules_from_rows([])) == 6, "")
cab = C.schedules_from_rows([{"Staff_User": {"id": "x"}, "Name": "O C", "Team": "Coordinator", "Active": True,
                              "Start_Hour": 10, "End_Hour": 19, "Days_Off": ["Sun", "Mon"], "Saturday_Hours": "10-17"}])[0]
sat = TODAY + timedelta(days=5)
ck("Saturday hours from Team_Schedules", C.shift_on(cab, sat) == (10, 17) and C.shift_on(cab, TODAY) is None, (C.shift_on(cab, sat), C.shift_on(cab, TODAY)))
slots = C.free_slots(SCHED[0], [NOW + timedelta(minutes=30)], NOW, TZ)
st = [datetime.fromisoformat(x["at"]) for x in slots]
ck("call-back slots: 15+ minutes away, inside her shift, 10 minutes clear of a booked call",
   st and min(st) >= NOW + timedelta(minutes=15) and all(12 <= x.astimezone(TZ).hour < 21 for x in st)
   and all(abs((x - (NOW + timedelta(minutes=30))).total_seconds()) >= 600 for x in st), slots[:3])

# ── the app: gate, routing, writes ─────────────────────────────────────────
import app as A  # noqa: E402

c = A.app.test_client()
USERS = [{"id": ME, "name": "Jorge Mier", "email": "jorge@goalsplasticsurgery.com"},
         {"id": OTHER, "name": "Judith Merlo", "email": "judith@goalsplasticsurgery.com"},
         {"id": "u1", "name": "Jessica Morales", "email": "jess@goalsplasticsurgery.com"}]
writes, gets_ = [], []
CALL_INFO = {
    "91001": {"Owner": {"id": ME}, "Outgoing_Call_Status": "Overdue", "What_Id": {"id": "j1"}, "$se_module": "Journeys"},
    "91005": {"Owner": {"id": OTHER}, "Outgoing_Call_Status": "Scheduled", "What_Id": {"id": "j5"}, "$se_module": "Journeys"},
    "91011": {"Owner": {"id": ME}, "Outgoing_Call_Status": "Cancelled", "What_Id": {"id": "j4"}, "$se_module": "Journeys"},
    "91012": {"Owner": {"id": ME}, "Outgoing_Call_Status": "Scheduled", "What_Id": {"id": "d1"}, "$se_module": "Deals"},
    "91009": {"Owner": {"id": ME}, "Outgoing_Call_Status": "Completed", "What_Id": {"id": "j3"}, "$se_module": "Journeys"},
}
saved = (A.GOOGLE_CLIENT_ID, A.MY_DAY_FAKE, A._zoho.list_users_detail, A._zoho.coord_schedules, A._zoho.coord_world,
         A._zoho.coord_get_call, A._zoho.coord_write_result, A._zoho.coord_create_call, A._zoho.my_day_world)
try:
    import copy
    A.GOOGLE_CLIENT_ID = "x"
    A.MY_DAY_FAKE = False
    A._md_users.update(at=0.0, rows=None)
    A._cd_sched.update(at=0.0, rows=None)
    A._cd_state.update(at=0.0, world=None, refreshing=False, error="")
    A._cd_recent.clear()
    A._zoho.list_users_detail = lambda: USERS
    A._zoho.coord_schedules = lambda: [
        {"Staff_User": {"id": ME}, "Name": "Jorge Mier", "Team": "Coordinator", "Active": True, "Start_Hour": 0, "End_Hour": 24, "Days_Off": []},
        {"Staff_User": {"id": OTHER}, "Name": "Judith Merlo", "Team": "Coordinator", "Active": True, "Start_Hour": 0, "End_Hour": 24, "Days_Off": []}]
    LIVE = copy.deepcopy(WORLD)
    # the live clock, not NOW: the app builds for "now"
    for cc in LIVE["calls"]:
        if cc["id"] == "91001":
            cc["Call_Start_Time"] = (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat()
    reads = []
    A._zoho.coord_world = lambda ids, a, b: reads.append(list(ids)) or copy.deepcopy(LIVE)
    A._zoho.coord_get_call = lambda cid: gets_.append(cid) or dict(CALL_INFO.get(cid, {}))

    def fake_write(cid, result, complete):
        writes.append(("result", cid, result, complete))
        return {"Call_Result": result}

    def fake_create(jid, owner, subject, at_iso, desc):
        writes.append(("create", jid, owner, subject, at_iso))
        return "newcall1"

    A._zoho.coord_write_result = fake_write
    A._zoho.coord_create_call = fake_create

    ck("closed without the door", c.get("/my-day/api/coord/data").status_code == 401, "")
    ck("the floor page is managers only", c.get("/my-day/coordinators").status_code in (200, 302) and b"BOOT" not in c.get("/my-day/coordinators").data, "")

    with c.session_transaction() as s:
        s["user"] = {"email": "jorge@goalsplasticsurgery.com", "name": "Jorge Mier"}
    r = c.get("/my-day")
    html = r.get_data(as_text=True)
    ck("a coordinator signed in gets the coordinator My Day", r.status_code == 200 and "/my-day/api/coord/data" in html, r.status_code)
    dd = c.get("/my-day/api/coord/data").get_json()
    ck("...built from the floor read of the Team_Schedules coordinators", dd.get("coord", {}).get("id") == ME and reads and set(reads[0]) == {ME, OTHER}, (dd.get("error"), reads))
    ck("...with her calls", [x["call_id"] for x in dd.get("right_now", [])][:1] == ["91001"], dd.get("right_now"))
    ck("she cannot open another coordinator's day", c.get("/my-day/api/coord/data?rep=" + OTHER).status_code == 403, "")

    def post(body, ctype="application/json"):
        return c.post("/my-day/api/coord/result", json=body) if ctype else c.post("/my-day/api/coord/result", data=body)

    rr = c.post("/my-day/api/coord/result", data={"rep": ME, "callId": "91001", "result": "reached"})
    ck("a write needs a JSON body", rr.status_code == 415, rr.status_code)
    rr = post({"rep": ME, "callId": "91001", "result": "confirmed"})
    ck("only the five results", rr.status_code == 400, rr.status_code)
    rr = post({"rep": ME, "callId": "91005", "result": "reached"})
    ck("never another coordinator's call", rr.status_code == 403 and not writes, (rr.status_code, writes))
    rr = post({"rep": ME, "callId": "91012", "result": "reached"})
    ck("never a call that is not on a Journey", rr.status_code == 400 and not writes, (rr.status_code, writes))
    rr = post({"rep": ME, "callId": "91011", "result": "reached"})
    ck("never a cancelled call", rr.status_code == 409 and not writes, (rr.status_code, writes))
    rr = post({"rep": ME, "callId": "91001", "result": "no_answer"})
    ck("No answer completes the open call with 'No Answer / Not Reached'",
       rr.get_json().get("ok") and writes[-1] == ("result", "91001", "No Answer / Not Reached", True), (rr.get_json(), writes))
    dd = c.get("/my-day/api/coord/data").get_json()
    ck("...and the page shows it done at once (no new Zoho read)", "91001" not in [x["call_id"] for x in dd["right_now"]]
       and any(x["call_id"] == "91001" and x["result"] == "No Answer / Not Reached" for x in dd["done"]) and len(reads) == 1, len(reads))
    A._cd_state.update(world=A._cd_reapply(copy.deepcopy(LIVE)))
    dd = c.get("/my-day/api/coord/data").get_json()
    ck("...and a refresh that read Zoho before the write does not bring it back", "91001" not in [x["call_id"] for x in dd["right_now"]], "")
    rr = post({"rep": ME, "callId": "91009", "result": "left_voicemail"})
    ck("a completed call with no result: the result only, not completed again", writes[-1] == ("result", "91009", "Left Voicemail", False), writes[-1])
    for key, val in (("reached", "Reached"), ("rescheduling", "Rescheduled Call")):
        CALL_INFO["91001"]["Outgoing_Call_Status"] = "Scheduled"
        post({"rep": ME, "callId": "91001", "result": key})
        ck("%s writes %r" % (key, val), writes[-1][2] == val, writes[-1])
    n0 = len(writes)
    rr = post({"rep": ME, "callId": "91001", "result": "call_back"})
    ck("a call back needs a time", rr.status_code == 400 and len(writes) == n0, rr.status_code)
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    rr = post({"rep": ME, "callId": "91001", "result": "call_back", "at": when.isoformat()})
    j = rr.get_json()
    ck("Wants a call back: 'Callback Requested' on the call, then a new call at the picked time on the same Journey",
       j.get("ok") and writes[-2][2] == "Callback Requested" and writes[-1][0] == "create" and writes[-1][1] == "j1"
       and writes[-1][2] == ME and writes[-1][4] == when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
       and " · Call back · " in writes[-1][3] and writes[-1][3].endswith("· P1"), (j, writes[-2:]))
    rr = post({"rep": ME, "callId": "91001", "result": "call_back", "at": (datetime.now(timezone.utc) + timedelta(days=40)).isoformat()})
    ck("a call back more than 3 weeks out is refused", rr.status_code == 400, rr.status_code)
    with c.session_transaction() as s:
        s.clear()
        s["v5_pw"] = True
    ck("the floor word reads but never writes", post({"rep": ME, "callId": "91001", "result": "reached"}).status_code == 403, "")

    # the reps' page is the reps' page
    with c.session_transaction() as s:
        s.clear()
        s["user"] = {"email": "jess@goalsplasticsurgery.com", "name": "Jessica Morales"}
    A._zoho.my_day_world = lambda *a, **k: {"calls_open": [], "calls_today": [], "deals_new": [], "deals_queue": [], "deals": {}, "deal_calls": [], "contacts": {}}
    html = c.get("/my-day").get_data(as_text=True)
    ck("a sales rep still gets the reps' My Day", "/my-day/api/data" in html and "/my-day/api/coord/data" not in html, "")
    A.MY_DAY_COORD = False
    with c.session_transaction() as s:
        s["user"] = {"email": "jorge@goalsplasticsurgery.com", "name": "Jorge Mier"}
    ck("MY_DAY_COORD=0 gives coordinators the old page back", "/my-day/api/coord/data" not in c.get("/my-day").get_data(as_text=True), "")
    A.MY_DAY_COORD = True

    # the floor
    with c.session_transaction() as s:
        s.clear()
        s["md_admin"] = True
    fl = c.get("/my-day/api/coord/floor").get_json()
    ck("the manager floor lists every coordinator with the four numbers",
       {x["id"] for x in fl.get("rows", [])} == {ME, OTHER} and all({"contact", "reach", "deadlines", "welcomes"} <= set(x["score"]) for x in fl["rows"]), fl)
    ck("...without patient lists (counts only)", "'quiet':" not in str(fl) and "'missing':" not in str(fl), "")
    ck("the floor page opens for a manager", c.get("/my-day/coordinators").status_code == 200, "")
finally:
    (A.GOOGLE_CLIENT_ID, A.MY_DAY_FAKE, A._zoho.list_users_detail, A._zoho.coord_schedules, A._zoho.coord_world,
     A._zoho.coord_get_call, A._zoho.coord_write_result, A._zoho.coord_create_call, A._zoho.my_day_world) = saved

src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
block = src[src.index("# ── Coordinator My Day"):src.index('@app.route("/v6/board")')]
ck("the result write logs ids, never a phone", "phone" not in block.split("log.info")[1][:200] if "log.info" in block else False, "")
ck("nothing in the coordinator block sends a text", not any(w in block.lower() for w in ("send_sms", "sendsms", "/sms", "telegram")), "")
zc = open(os.path.join(HERE, "zoho_client.py"), encoding="utf-8").read()
zb = zc[zc.index("Coordinator My Day"):zc.index("def my_day_busy")]
ck("coordinator writes go with trigger []", zb.count('"trigger": []') == 2, zb.count('"trigger": []'))
ck("coordinator writes never move or cancel a call", "Cancelled" not in zb and "Call_Start_Time\"] =" not in zb and "delete" not in zb.lower(), "")

for e in errors:
    print("FAIL:", e)
print("checks_coord_day: %d passed, %d failed" % (passed, len(errors)))
print("%d failed" % len(errors))
sys.exit(1 if errors else 0)
