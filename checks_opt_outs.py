"""STOP replies, honored -- the matcher, the scan, the list, the Zoho mark.

Danny, 2026-10-15: "make sure all stop replies are getting honored".
Run: ./.venv/bin/python -B checks_opt_outs.py   (tally LAST)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sms_opt_outs as so  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


# ---- the matcher: the CTIA keywords and the shapes people actually type ----
YES = ["STOP", "Stop ", "stop", "STOP!", "Stop.", "STOPALL", "UNSUBSCRIBE", "unsubscribe me", "Cancel", "END", "quit",
       "stop texting me", "Stop contacting me please", "please stop", "pls stop", "Please stop texting", "\U0001F6D1",
       "\U0001F6D1 ", "STOP\nReply STOP to opt out"]
NO = ["don't stop", "Dont stop", "stop by the office?", "I can't stop thinking about it", "Okay", "Yes", "", " ",
      "Can you stop by tomorrow?", "never stop", "I didn't say stop", "Stopped at the pharmacy, call you after",
      "What's the deal now?", "No", "Canceled", "cancellation fee?", "I will end up paying next week", "the end of the month works"]
for t in YES:
    ck("opt-out: %r" % t, so.is_opt_out(t) is True, t)
for t in NO:
    ck("NOT an opt-out: %r" % t, so.is_opt_out(t) is False, t)

# ---- the scan: inbound only, text asked for, one hit per STOP, a seat that cannot be read is a problem ----
def _row(i, d, frm, to, at, txt):
    return {"id": i, "direction": d, "start_time": at, "from_number": frm, "to_number": to, "text": txt}
_asked = []
def _fem(ext_id, s_, e_, max_pages=6, max_wait=0.0, timeout=15.0, keep_text=False):
    _asked.append((ext_id, keep_text))
    if ext_id == 3:
        raise RuntimeError("HTTP 429")
    if ext_id == 2:
        return [], {"note": "Hit the 4-page cap"}
    return [_row("a", "Inbound", "+13055550101", "+12125550100", "2026-10-15T14:00:00Z", "STOP"),
            _row("b", "Outbound", "+12125550100", "+13055550101", "2026-10-15T14:01:00Z", "STOP"),      # ours, ignored
            _row("c", "Inbound", "+13055550202", "+12125550100", "2026-10-15T15:00:00Z", "Yes please call me"),
            _row("d", "Inbound", "+13055550303", "+12125550100", "2026-10-15T15:30:00Z", "\U0001F6D1"),
            _row("e", "Inbound", "+13055550303", "+12125550100", "2026-10-15T15:31:00Z", "Stop")], {"note": None}
seats = [{"name": "Gabriel Johnson", "ext": "162", "ext_id": 1}, {"name": "Capped", "ext": "2", "ext_id": 2}, {"name": "Broken", "ext": "3", "ext_id": 3}]
hits, problems, outbound = so.scan(_fem, seats, datetime(2026, 10, 15, tzinfo=timezone.utc), datetime(2026, 10, 16, tzinfo=timezone.utc), pause=0)
ck("the text body is asked for on every seat", all(k for _, k in _asked) and len(_asked) == 3, _asked)
ck("three STOPs found: two numbers, one of them twice", [h["digits"] for h in hits] == ["3055550101", "3055550303", "3055550303"], hits)
ck("...with the seat, the time and only the first 40 characters", hits[0]["seat"] == "Gabriel Johnson" and hits[0]["at"] == "2026-10-15T14:00:00Z" and hits[0]["text"] == "STOP" and hits[0]["msg_id"] == "a", hits[0])
ck("our own outbound STOP line is never a hit", not any(h["digits"] == "2125550100" for h in hits), hits)
ck("a capped seat and a seat that errored are reported, not counted clean", [p["seat"] for p in problems] == ["Capped", "Broken"] and "429" in problems[1]["note"], problems)
ck("every text the seats SENT comes back too, for the honored check", outbound == [{"digits": "3055550101", "seat": "Gabriel Johnson", "at": "2026-10-15T14:01:00Z", "status": ""}], outbound)

# Honored = nothing sent to the number after its STOP, from ANY seat.
_nums = {"3055550101": {"first_seen": "2026-10-15T14:00:00Z"}, "3055550303": {"first_seen": "2026-10-15T15:30:00Z"}}
_out = [{"digits": "3055550101", "seat": "A", "at": "2026-10-15T13:59:00Z", "status": "Delivered"},   # before: fine
        {"digits": "3055550101", "seat": "B", "at": "2026-10-15T16:00:00Z", "status": "Delivered"},   # after, another seat
        {"digits": "3055550303", "seat": "A", "at": "2026-10-15T17:00:00Z", "status": "SendingFailed"},  # attempted counts
        {"digits": "3055550999", "seat": "A", "at": "2026-10-15T18:00:00Z", "status": "Delivered"}]   # not on the list
_aft = so.texted_after_stop(_nums, _out)
ck("a text after the STOP is caught from another seat, a failed attempt counts, newest first",
   [(o["digits"], o["seat"]) for o in _aft] == [("3055550303", "A"), ("3055550101", "B")], _aft)

# ---- the list: deduped by message, new numbers reported once, survives a reload ----
tmp = os.path.join(tempfile.mkdtemp(), "sms_opt_outs.json")
st = so.Store(tmp)
new1 = st.add(hits)
ck("first fold: both numbers are new", sorted(new1) == ["3055550101", "3055550303"], new1)
new2 = st.add(hits)
ck("the same hits again add nothing new, and the message ids do not double", new2 == [] and st.data["numbers"]["3055550303"]["msg_ids"] == ["d", "e"], st.data["numbers"]["3055550303"])
ck("last_seen is the latest STOP, first_seen the earliest", st.data["numbers"]["3055550303"]["first_seen"] == "2026-10-15T15:30:00Z" and st.data["numbers"]["3055550303"]["last_seen"] == "2026-10-15T15:31:00Z", st.data["numbers"]["3055550303"])
st.save()
st2 = so.Store(tmp)
ck("the list comes back from disk", st2.digits() == ["3055550101", "3055550303"], st2.digits())
_ap = os.path.join(_tmp_armed := tempfile.mkdtemp(), "o.json")
_sa = so.Store(_ap); _sa.data["zoho_armed"] = True; _sa.save()
ck("the armed flag survives a restart (a reload used to drop it: every deploy disarmed the Zoho marking)",
   so.Store(_ap).data.get("zoho_armed") is True, so.Store(_ap).data)
ck("the plain list is sorted digits, nothing else", st2.digits() == sorted(st2.data["numbers"]), st2.digits())

# ---- Zoho: a STOP is the whole unsubscribe, trigger: [] (Danny 2026-10-25) ----
class _Resp:
    def __init__(self, status, body): self.status_code, self._b, self.ok, self.content, self.text = status, body, status < 300, b"x", ""
    def json(self): return self._b
_calls = []
class _Http:
    @staticmethod
    def post(url, headers=None, json=None, timeout=0):
        q = json["select_query"]; _calls.append(("post", q))
        if "from Contacts" in q: return _Resp(200, {"data": [{"id": "c1", "Phone": "+1 305-555-0101"}, {"id": "c2", "Mobile": "13055550101"},
                                                              {"id": "cX", "Phone": "3055550101 ext 9"}]})
        if "from Leads" in q: return _Resp(204, {})
        if "from Journeys" in q: return _Resp(200, {"data": [{"id": "j1", "Contact": {"id": "c2"}}]})
        if "from Deals where Contact_Name" in q: return _Resp(200, {"data": [{"id": "d1", "Stage": "Quote Sent"}, {"id": "d2", "Stage": "Closed Won - Surgery Scheduled"}]})
        if "from Deals where" in q and "Contact_Name" not in q: return _Resp(200, {"data": [{"id": "d3", "Stage": "Call Scheduled", "Phone": "(305) 555-0101"}, {"id": "dX", "Stage": "New Deal", "Phone": "3055550101 ext 9"}]})
        if "from Calls" in q and "Who_Id" in q: return _Resp(200, {"data": [{"id": "k1", "Outgoing_Call_Status": "Scheduled", "Description": "Quote follow up"}]})
        if "from Calls" in q and "What_Id" in q: return _Resp(200, {"data": [{"id": "k1", "Outgoing_Call_Status": "Scheduled", "Description": "Quote follow up"}, {"id": "k2", "Outgoing_Call_Status": "Overdue", "Description": None}]})
        return _Resp(400, {})
    @staticmethod
    def put(url, headers=None, json=None, timeout=0):
        _calls.append(("put", url.rsplit("/", 1)[-1], json))
        return _Resp(200, {"data": [{"code": "SUCCESS"} for _ in json["data"]]})
res = so.mark_in_zoho("https://z", lambda: {"Authorization": "x"}, "3055550101", "2026-10-15T21:30:00-04:00", _Http)
puts = {}
for c in _calls:
    if c[0] == "put": puts.setdefault(c[1], []).append(c[2])
P = {k: v[0] for k, v in puts.items()}
ck("contacts, deals (through the Contact AND by phone), the Active Journey and the open calls are found; nothing raised",
   res["found"] == {"contacts": 2, "leads": 0, "deals": 3, "journeys": 1, "calls": 2} and res["errors"] == [], res)
ck("every write carries trigger: []", all(j["trigger"] == [] for v in puts.values() for j in v), {k: [j["trigger"] for j in v] for k, v in puts.items()})
c1 = P["Contacts"]["data"][0]
ck("the Contact gets Do_Not_Contact, the SMS and email opt-outs, the Unsubscribed stamp and the Deal_Stage mirror",
   c1 == {"id": "c1", "ringcentralextensionabr__SMS_OPT_OUT": True, "Email_Opt_Out": True, "Unsubscribed_Mode": "Manual",
          "Unsubscribed_Time": "2026-10-15T21:30:00-04:00", "Do_Not_Contact": True, "Deal_Stage": "Unsubscribe"}, c1)
c2 = P["Contacts"]["data"][1]
ck("a Contact with an Active Journey keeps the opt-outs but NOT Do_Not_Contact (the coordinator must reach a booked patient)",
   c2["id"] == "c2" and "Do_Not_Contact" not in c2 and c2["ringcentralextensionabr__SMS_OPT_OUT"] is True and res.get("do_not_contact") == 1, c2)
ck("...and her Active Journey's texts are switched off instead",
   P["Journeys"]["data"] == [{"id": "j1", "Send_SMS": False, "Hold_Send_SMS_Follow_Up": True}] and res["journeys"] == 1, P.get("Journeys"))
D = {d["id"]: d for d in P["Deals"]["data"]}
ck("open sales deals move to Unsubscribe with every SMS switch off",
   D["d1"].get("Stage") == "Unsubscribe" and D["d3"].get("Stage") == "Unsubscribe" and D["d1"]["RC_SMS_Opt_Out"] is True
   and D["d1"]["Send_Welcome_SMS"] is False and D["d1"]["New_Deal_24H_SMS"] is False, D)
ck("a paid patient's deal keeps its stage but loses its SMS switches too",
   "Stage" not in D["d2"] and D["d2"]["RC_SMS_Opt_Out"] is True and D["d2"]["Send_Welcome_SMS"] is False and res["deals_moved"] == 2, D["d2"])
ck("a deal whose phone only shares the last digits is not touched", "dX" not in D, sorted(D))
K = {c["id"]: c for c in P["Calls"]["data"]}
ck("every Scheduled / Overdue call on the person or her deals is Cancelled, once, with the reason kept under the old notes",
   sorted(K) == ["k1", "k2"] and all(c["Outgoing_Call_Status"] == "Cancelled" for c in K.values())
   and K["k1"]["Description"] == "Quote follow up\n\nCancelled: patient texted STOP (2026-10-15)."
   and K["k2"]["Description"] == "Cancelled: patient texted STOP (2026-10-15)." and res["calls_cancelled"] == 2, K)
ck("no Leads write when no lead matched", "Leads" not in P, sorted(P))
ck("the number is looked up by its full ten digits, never the last seven alone",
   "like '%3055550101'" in _calls[0][1] and "like '%5550101'" not in _calls[0][1], _calls[0][1])
ck("...and in the formats Zoho stores it in, so '(305) 555-0101' is found too (678-472-3509 was not, 10-23..10-25)",
   "'(305) 555-0101'" in _calls[0][1] and "'305-555-0101'" in _calls[0][1] and "'+13055550101'" in _calls[0][1]
   and "Mobile in (" in _calls[0][1], _calls[0][1][:300])
ck("...and a field whose digits do not END in them is dropped", [d["id"] for d in P["Contacts"]["data"]] == ["c1", "c2"], P["Contacts"]["data"])
_calls.clear()
class _Down(_Http):
    @staticmethod
    def post(url, headers=None, json=None, timeout=0): return _Resp(500, {})
res2 = so.mark_in_zoho("https://z", lambda: {}, "3055550101", "x", _Down)
ck("a Zoho outage is an error on the record, not an exception", res2["errors"] and res2["errors"][0]["module"] == "lookup" and "500" in res2["errors"][0]["detail"], res2)
ck("the Zoho stamp is the org's local offset", so.now_iso_local(240).endswith("-04:00") and so.now_iso_local(-60).endswith("+01:00"), (so.now_iso_local(240), so.now_iso_local(-60)))

# ---- the app: the loop is registered, the routes gate on sign-in, the list file lives on the data disk ----
import app as A  # noqa: E402
ck("the hourly loop is one of the worker loops", any(n == "loop:optouts" for n, _ in A._WORKER_LOOPS), [n for n, _ in A._WORKER_LOOPS])
ck("the list file sits on the data disk next to the roster override", A._OPTOUT_FILE.name == "sms_opt_outs.json" and A._OPTOUT_FILE.parent == A.BILLING_ROSTER_FILE.parent, str(A._OPTOUT_FILE))
ck("the first scan waits ten minutes after a boot, so the boards' texts load first",
   "def _optouts_loop():" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read().split("time.sleep(600)")[0][-900:], "boot delay")
ck("every texting team is scanned, the coordinator trainees too (2026-10-26)", A._OPTOUT_TEAMS == ("billing", "surgical", "sales", "trainees"), A._OPTOUT_TEAMS)
_c = A.app.test_client()
_real_allowed = A._v6_allowed
A._v6_allowed = lambda: False
try:
    r1 = _c.get("/api/v6/opt-outs"); r2 = _c.get("/api/v6/opt-outs.txt")
finally:
    A._v6_allowed = _real_allowed
ck("signed out, both routes refuse", r1.status_code == 401 and r2.status_code == 401, (r1.status_code, r2.status_code))
A._v6_allowed = lambda: True
try:
    A._optouts.data["numbers"]["3055550909"] = {"first_seen": "2026-10-15T14:00:00Z", "last_seen": "2026-10-15T14:00:00Z", "seat": "Test", "ext": "1", "text": "STOP", "msg_ids": ["z"], "zoho": None}
    j = _c.get("/api/v6/opt-outs").get_json(); t = _c.get("/api/v6/opt-outs.txt").get_data(as_text=True)
finally:
    A._v6_allowed = _real_allowed
    A._optouts.data["numbers"].pop("3055550909", None)
ck("signed in, the list and the plain file carry the number", any(n["number"] == "3055550909" for n in j.get("numbers", [])) and "3055550909\n" in t, (j.get("count"), t[:60]))

# Shipped disarmed: a run collects but writes nothing to Zoho until ?arm=1.
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
ck("Zoho writes need the list to be armed, and the env switch still overrides",
   "write = os.environ.get(\"OPT_OUT_ZOHO_WRITE\", \"1\") != \"0\" and bool(_optouts.data.get(\"zoho_armed\"))" in _src
   and "if write and _zoho.refresh_token and _zoho.client_id:" in _src, "arming gate")
ck("a fresh list is disarmed", not so.Store(os.path.join(tempfile.mkdtemp(), "x.json")).data.get("zoho_armed"), "armed by default")
ck("the hourly scan reads 20 pages a seat, so a blast line's STOPs are not past the cap",
   "seats, start_dt, end_dt,\n                                                     max_pages=20)" in _src
   and "after = _optouts_mod.texted_after_stop(_optouts.data[\"numbers\"], outbound)" in _src and "\"texted_after_stop\": len(after)" in _src, "cap or honored check")
A._v6_allowed = lambda: True
try:
    r404 = _c.get("/api/v6/opt-outs?mark=3055559999")
finally:
    A._v6_allowed = _real_allowed
ck("?mark= on a number that is not on the list is refused", r404.status_code == 404, r404.status_code)

# ---- 2026-10-25: "STP" is a STOP; the shared office lines are scanned too ----
ck('"STP" alone is an opt-out', so.is_opt_out("STP") is True, so.is_opt_out("STP"))
ck('"stp" is not read into a longer sentence', so.is_opt_out("can you stp by the office") is False, so.is_opt_out("can you stp by the office"))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
import app as A  # noqa: E402
OPT = A._optouts_mod
_asked = []
_saved_scan, _saved_panel, _saved_seats = OPT.scan, A._sms_panel_seats, A._sms_seats
A._sms_seats = lambda team, names=(): ([{"name": "Seat " + team, "ext": "1", "ext_id": "ex-" + team}], [])
A._sms_panel_seats = lambda: ([{"label": "Salome", "name": "Salome", "ext": "151", "ext_id": "220135049"},
                               {"label": "Ariel", "name": "Ariel Voskin", "ext": "689", "ext_id": "680145035"}], ["Umed"])
def _fake_scan(fetch, seats, start_dt, end_dt, max_pages=4, timeout=15.0, pause=0.3):
    _asked.extend(x["ext_id"] for x in seats); return [], [], []
OPT.scan = _fake_scan
try:
    A._optouts_run(days=1)
finally:
    OPT.scan, A._sms_panel_seats, A._sms_seats = _saved_scan, _saved_panel, _saved_seats
ck("the hourly STOP scan reads the shared office lines after the rosters",
   _asked[-2:] == ["220135049", "680145035"] and "ex-billing" in _asked, _asked)
ck("a shared line the directory lacks is reported, not dropped silently",
   any("Umed" in str(u) for u in (A._optouts_state.get("last") or {}).get("unresolved", [])), (A._optouts_state.get("last") or {}).get("unresolved"))

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
