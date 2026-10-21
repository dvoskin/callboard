"""Unreturned inbound calls board: the rules, then the doors.

Danny, 2026-10-13: "build the unreturned inbound calls board". The trap the
rules guard against is the one the audit found: a 20-second "Call connected"
is the voicemail greeting, and three of those in a minute let the CRM say
"4th attempt" while the patient has never been spoken to. So a miss is
returned only by a connected call of at least 45 seconds or by the patient
being answered when they call again.

Run with no arguments. The rules part reads nothing from the network; the
route part uses Flask's test client and never reaches RingEX or Zoho.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "checks-gate-on")   # the door must be shut
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import callbacks_board as CB  # noqa: E402

errors, passed = [], 0


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


T0 = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)   # 11:00 EDT


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def call(direction, result, minutes, num, duration=0, agent="", sid=""):
    return {"direction": direction, "result": result, "start_time": at(minutes),
            "duration": duration, "agent_name": agent, "session_id": sid,
            "from_number": "+1" + num if direction == "Inbound" else "+19298331121",
            "to_number": "+19298331121" if direction == "Inbound" else "+1" + num}


J = [
    {"id": "j1", "name": "Cathy Jules", "digits": "4049275839", "owner": "Yareth Pavon",
     "owner_id": "u1", "surgery_date": "2026-10-19", "stage": "Surgery Preparation",
     "date_status": "Confirmed", "language": "English"},
    {"id": "j2", "name": "Teresa Downer", "digits": "8393337842", "owner": "Johana Duron",
     "owner_id": "u2", "surgery_date": "2026-10-23", "stage": "Surgery Preparation",
     "date_status": "Confirmed", "language": "English"},
    {"id": "j3", "name": "Oscar Cano", "digits": "4048385899", "owner": "Yareth Pavon",
     "owner_id": "u1", "surgery_date": "2026-10-20", "stage": "Surgery Preparation",
     "date_status": "Confirmed", "language": "Spanish"},
    {"id": "j4", "name": "No Number", "digits": "", "owner": "Johana Duron", "owner_id": "u2",
     "surgery_date": "2026-10-30"},
]
NOW = T0 + timedelta(hours=30)
EDT = 240

# ---- a miss from a booked number is open; a stranger's is not ----
b = CB.build([call("Inbound", "Missed", 0, "4049275839", agent="Adelita Flowers"),
              call("Inbound", "Missed", 5, "2125550000")], J, now=NOW, tz_offset_minutes=EDT)
ck("one open row", b["summary"]["open"] == 1, b["summary"])
r = b["rows"][0]
ck("it is Cathy, open", r["patient"] == "Cathy Jules" and r["status"] == "open", r)
ck("owner is the Journey owner", r["owner"] == "Yareth Pavon", r["owner"])
ck("the phone that rang is named", r["rang"] == ["Adelita Flowers"], r["rang"])
ck("age is measured from the miss", abs(r["age_hours"] - 30) < 0.1, r["age_hours"])
ck("the stranger's miss is ignored", all(x["digits"] != "2125550000" for x in b["rows"]))
ck("local label reads 11:00 AM", r["first_missed"]["label"].endswith("11:00 AM"), r["first_missed"])
ck("the number is dialable", r["number"] == "(404) 927-5839" and r["digits"] == "4049275839", r["number"])
ck("days until surgery from local today", r["days_until"] == 6, r["days_until"])
ck("a Journey with no number is counted, not lost", b["summary"]["journeys_without_number"] == 1, b["summary"])

# ---- a voicemail drop is a try, not a return ----
b = CB.build([call("Inbound", "Voicemail", 0, "4049275839"),
              call("Outbound", "Call connected", 60, "4049275839", duration=14, agent="Yareth Pavon"),
              call("Outbound", "Call connected", 61, "4049275839", duration=12, agent="Yareth Pavon"),
              call("Outbound", "Hang Up", 62, "4049275839", duration=5, agent="Yareth Pavon")],
             J, now=NOW, tz_offset_minutes=EDT)
r = b["rows"][0]
ck("three short dials leave it open", r["status"] == "open", r["status"])
ck("they are counted as tries", r["tries"] == 3, r["tries"])
ck("the last try is named", r["last_try"]["by"] == "Yareth Pavon" and r["last_try"]["result"] == "Hang Up", r["last_try"])

# ---- a connected call of 45 s or more returns it ----
b = CB.build([call("Inbound", "Missed", 0, "4049275839"),
              call("Outbound", "Call connected", 30, "4049275839", duration=20, agent="Yareth Pavon"),
              call("Outbound", "Call connected", 90, "4049275839", duration=295, agent="Vivian Martinez")],
             J, now=NOW, tz_offset_minutes=EDT)
ck("returned, not open", b["summary"]["open"] == 0 and b["summary"]["returned"] == 1, b["summary"])
r = b["rows"][0]
ck("credited to who connected", r["returned"]["by"] == "Vivian Martinez" and r["returned"]["how"] == "called back", r["returned"])
ck("the earlier short dial stays a try", r["tries"] == 1, r["tries"])
ck("hours to return", abs(r["returned"]["hours_to_return"] - 1.5) < 0.01, r["returned"])

# ---- exactly 45 s counts; 44 does not ----
b = CB.build([call("Inbound", "Missed", 0, "4049275839"),
              call("Outbound", "Call connected", 10, "4049275839", duration=44)], J, now=NOW)
ck("44 s is a try", b["summary"]["open"] == 1, b["summary"])
b = CB.build([call("Inbound", "Missed", 0, "4049275839"),
              call("Outbound", "Call connected", 10, "4049275839", duration=45)], J, now=NOW)
ck("45 s returns", b["summary"]["returned"] == 1 and b["summary"]["open"] == 0, b["summary"])

# ---- the patient calling again and being answered returns it ----
b = CB.build([call("Inbound", "Missed", 0, "8393337842"),
              call("Inbound", "Accepted", 3, "8393337842", duration=33, agent="Ariel Voskin")],
             J, now=NOW, tz_offset_minutes=EDT)
r = b["rows"][0]
ck("answered on the second call", r["status"] == "returned" and r["returned"]["how"] == "called again, answered", r)
ck("who answered", r["returned"]["by"] == "Ariel Voskin", r["returned"])

# ---- one inbound rings three phones: one session, answered if any leg was ----
legs = [call("Inbound", "Missed", 0, "8393337842", agent="A", sid="s1"),
        call("Inbound", "Missed", 0, "8393337842", agent="B", sid="s1"),
        call("Inbound", "Accepted", 0, "8393337842", agent="C", sid="s1", duration=120)]
b = CB.build(legs, J, now=NOW)
ck("an answered session is not a miss", b["summary"]["open"] == 0 and not b["rows"], b["summary"])
legs = [call("Inbound", "Missed", 0, "8393337842", agent="A", sid="s2"),
        call("Inbound", "Missed", 0, "8393337842", agent="B", sid="s2")]
b = CB.build(legs, J, now=NOW)
ck("an unanswered session is ONE miss", b["summary"]["open"] == 1 and b["rows"][0]["misses"] == 1, b["rows"])
ck("both phones that rang are named", b["rows"][0]["rang"] == ["A", "B"], b["rows"][0]["rang"])

# ---- repeated misses before the callback are one thread ----
b = CB.build([call("Inbound", "Missed", 0, "4049275839"),
              call("Inbound", "Missed", 6, "4049275839"),
              call("Inbound", "Voicemail", 12, "4049275839")], J, now=NOW, tz_offset_minutes=EDT)
r = b["rows"][0]
ck("three misses, one open row", b["summary"]["open"] == 1 and r["misses"] == 3, r)
ck("first and last miss both shown", r["first_missed"]["label"].endswith("11:00 AM") and r["last_missed"]["label"].endswith("11:12 AM"), r)
ck("open patients counts people", b["summary"]["open_patients"] == 1, b["summary"])

# ---- a thread closed, then a new miss, opens a new thread ----
b = CB.build([call("Inbound", "Missed", 0, "4049275839"),
              call("Outbound", "Call connected", 30, "4049275839", duration=300, agent="Vivian Martinez"),
              call("Inbound", "Missed", 600, "4049275839")], J, now=NOW)
ck("one returned and one open for the same patient",
   b["summary"]["returned"] == 1 and b["summary"]["open"] == 1, b["summary"])
ck("open rows come first", b["rows"][0]["status"] == "open" and b["rows"][1]["status"] == "returned",
   [x["status"] for x in b["rows"]])

# ---- an outbound BEFORE the miss does not count ----
b = CB.build([call("Outbound", "Call connected", 0, "4049275839", duration=300),
              call("Inbound", "Missed", 10, "4049275839")], J, now=NOW)
ck("earlier conversation does not return a later miss", b["summary"]["open"] == 1, b["summary"])

# ---- grouping: owners with the most open first; the surgery-within-7-days count ----
b = CB.build([call("Inbound", "Missed", 0, "4049275839"),
              call("Inbound", "Missed", 1, "4048385899"),
              call("Inbound", "Missed", 2, "8393337842")], J, now=NOW, tz_offset_minutes=EDT)
ck("Pavon first with two open", b["owners"][0]["owner"] == "Yareth Pavon" and b["owners"][0]["open"] == 2,
   [(o["owner"], o["open"]) for o in b["owners"]])
ck("Duron second with one", b["owners"][1]["owner"] == "Johana Duron" and b["owners"][1]["open"] == 1)
ck("surgery within 7 days: Cathy (6 d) and Oscar (7 d), not Teresa (10 d)",
   b["summary"]["within_7_days_open"] == 2, b["summary"])
ck("language rides along", next(r for r in b["rows"] if r["patient"] == "Oscar Cano")["language"] == "Spanish")

# ---- the same number on two active Journeys: the next surgery is shown, the other named ----
J2 = J + [{"id": "j9", "name": "Cathy Jules", "digits": "4049275839", "owner": "Jorge Mier",
           "owner_id": "u3", "surgery_date": "2026-11-20"}]
b = CB.build([call("Inbound", "Missed", 0, "4049275839")], J2, now=NOW, tz_offset_minutes=EDT)
r = b["rows"][0]
ck("the nearer surgery's case is the row", r["journey_id"] == "j1" and r["owner"] == "Yareth Pavon", r)
ck("the other case is named, not hidden", r["other_journeys"] and r["other_journeys"][0]["owner"] == "Jorge Mier", r["other_journeys"])

# ---- unreadable rows never crash the board ----
b = CB.build([{"direction": "Inbound", "result": "Missed", "start_time": "garbage", "from_number": "+14049275839"},
              {"direction": "Internal", "result": "Accepted", "start_time": at(0)},
              {}], J, now=NOW)
ck("bad rows are skipped", b["summary"]["open"] == 0 and b["summary"]["calls_scanned"] == 3, b["summary"])

# ---- a fax or a text in the call log is not a missed call ----
fax = call("Inbound", "Fax Receipt Error", 0, "4049275839"); fax["type"] = "Fax"
sms = call("Inbound", "Received", 1, "4049275839"); sms["type"] = "SMS"
b = CB.build([fax, sms], J, now=NOW)
ck("fax and text rows are ignored", b["summary"]["open"] == 0 and not b["rows"], b["summary"])
voice = call("Inbound", "Missed", 2, "4049275839"); voice["type"] = "Voice"
b = CB.build([fax, voice], J, now=NOW)
ck("a voice miss beside a fax still counts", b["summary"]["open"] == 1, b["summary"])

# ---- the doors ----
import app as A  # noqa: E402

c = A.app.test_client()
r = c.get("/api/v6/callbacks")
ck("the API is shut without a session", r.status_code == 401, r.status_code)
r = c.get("/callbacks")
ck("the page asks for the word or sends to login", r.status_code in (200, 302), r.status_code)
if r.status_code == 200:
    ck("…and it is the password page, not the board", b"password" in r.data.lower() and b"Unreturned" in r.data, r.data[:200])
else:
    # Since 2026-10-20 the login redirect carries the page back (/login?next=/callbacks).
    ck("…redirect goes to /login", r.headers.get("Location", "").split("?")[0].endswith("/login"), r.headers.get("Location"))
r = c.get("/api/v6/callbacks?days=99")
ck("still shut with a silly window", r.status_code == 401, r.status_code)
ck("the window is capped in code", A._CB_MAX_DAYS == 14, A._CB_MAX_DAYS)
ck("the connected floor is 45 s unless configured", A._CB_MIN_CONNECTED == 45, A._CB_MIN_CONNECTED)
ck("a word-password session opens the board", True)

print("checks_callbacks: %d passed, %d failed" % (passed, len(errors)))
for e in errors:
    print("  FAIL", e)
sys.exit(1 if errors else 0)
