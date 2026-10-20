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
         A._snapshot_ringex)
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
    r = c.post("/my-day/api/claim", json={"dealId": "555"})
    ck("a queue lead someone else claimed stops the call", r.get_json().get("go") is False
       and "Mabel" in r.get_json().get("message", ""), r.get_json())

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

    # unconfigured back office reads as coming soon, not a crash
    A.BACKOFFICE_URL = ""
    r = c.post("/my-day/api/outcome", json={"rep": "u2", "dealId": "123", "outcome": "no_answer"})
    ck("no BACKOFFICE_URL: 'Results are coming soon'", r.get_json().get("coming_soon") is True, r.get_json())
finally:
    (A.GOOGLE_CLIENT_ID, A.MY_DAY_FAKE, A.BACKOFFICE_URL, A.OVERDUE_API_KEY, A.MY_DAY_MANAGERS,
     A._zoho.list_users_detail, A._zoho.my_day_world, A.requests.post, A._load_ringex_snapshot,
     A._snapshot_ringex) = saved

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
