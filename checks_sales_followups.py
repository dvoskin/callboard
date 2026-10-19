"""The sales follow-up tracker: each rep's scheduled Zoho follow-up calls as a
progress hairline on /v5 and /teams.

Danny, 2026-10-19: "Add a follow up tracker to track scheduled calls assigned
to each coordinator on the sales trackers in a progress bar format like the
others".
Run: ./.venv/bin/python -B checks_sales_followups.py   (tally LAST)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app as A  # noqa: E402
import zoho_client as Z  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


# ---- the classification, on a fixed Zoho page ----
def call(i, owner, subject, status, start, created, what="D1", who="C1", ctype="Outbound"):
    return {"id": str(i), "Owner": {"id": owner}, "Subject": subject, "Call_Type": ctype,
            "Outgoing_Call_Status": status, "Call_Start_Time": start, "Created_Time": created,
            "What_Id": {"id": what} if what else None, "Who_Id": {"id": who} if who else None}


T = "2026-10-19T%s:00-04:00"
ROWS = [
    # logged by the phone systems: never follow-ups, only dials
    call(1, "U1", "Outbound call to Patient One", "Completed", T % "09:00", T % "09:01", what="D9"),
    call(2, "U1", "Outgoing call to (305) 555-0100", "Completed", T % "09:05", T % "09:06", what="D8"),
    call(3, "U1", "Inbound call from Patient", None, T % "09:10", T % "09:11", ctype="Inbound"),
    # follow-ups
    call(10, "U1", "Scheduled Call : Automated Follow Up 1 of 3 - Patient Two", "Completed", T % "10:00", T % "08:00", what="D2"),
    call(11, "U1", "Scheduled Call : Automated Follow Up 2 of 3 - Patient Three", "Cancelled", T % "10:30", T % "08:00", what="D3"),
    # open in Zoho, but U1 dialed D4 at 11:50 for an 12:00 follow-up made at 08:00 -> called
    call(12, "U1", "Scheduled Call : Quote Follow Up 1 - 2 Patient Four", "Overdue", T % "12:00", T % "08:00", what="D4"),
    call(13, "U1", "Outbound call to Patient Four", "Completed", T % "11:50", T % "11:51", what="D4"),
    # the dial that MADE the follow-up (a minute before it was created) does not complete it
    call(14, "U1", "Outbound call to Patient Five", "Completed", T % "11:00", T % "11:00", what="D5"),
    call(15, "U1", "Scheduled Call : Automated Follow Up 1 of 3 - Patient Five", "Scheduled", T % "13:00", T % "11:01", what="D5"),
    # a dial two hours early is not this follow-up
    call(16, "U1", "Outbound call to Patient Six", "Completed", T % "09:30", T % "09:31", what="D6"),
    call(17, "U1", "Call scheduled with Patient Six", "Overdue", T % "12:30", T % "08:00", what="D6"),
    # somebody else's dial does not count for U1
    call(18, "U2", "Outbound call to Patient Seven", "Completed", T % "13:30", T % "13:31", what="D7"),
    call(19, "U1", "Scheduled Call : Automated Follow Up 3 of 3 - Patient Seven", "Overdue", T % "13:30", T % "08:00", what="D7"),
    # due later today
    call(20, "U1", "Scheduled Call : Automated Follow Up 1 of 3 - Patient Eight", "Scheduled", T % "17:00", T % "12:00", what="D10"),
    # matched through the contact when the deal differs
    call(21, "U2", "Scheduled Call : Automated Follow Up 1 of 3 - Patient Nine", "Scheduled", T % "13:00", T % "08:00", what="D11", who="C9"),
    call(22, "U2", "Outbound call to Patient Nine", "Completed", T % "13:10", T % "13:11", what=None, who="C9"),
]
SENT = []


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code, self.ok, self.text = body, status, status < 400, json.dumps(body)

    def json(self):
        return self._b


def fake_post(url, headers=None, json=None, timeout=None):
    SENT.append(json["select_query"])
    return _Resp({"data": ROWS, "info": {"more_records": False}})


z = Z.ZohoClient.__new__(Z.ZohoClient)
z.base_url = "https://example.invalid"
z._headers = lambda: {}
real_post = Z.requests.post
Z.requests.post = fake_post
try:
    now = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc).astimezone(timezone.utc).replace(hour=18)   # 14:00 ET
    out = z.follow_up_breakdown(T % "00:00", T % "23:59", ["U1", "U2"], now=now)
finally:
    Z.requests.post = real_post
u1, u2 = out.get("U1") or {}, out.get("U2") or {}
ck("a logged call is never a follow-up: U1 has 7 scheduled ones, 6 not cancelled",
   u1.get("due") == 6 and u1.get("cancelled") == 1, u1)
ck("Completed in Zoho is done (marked)", u1.get("marked") == 1, u1)
ck("open in Zoho but dialed after it was made and no earlier than 30 min before its time: done (called)",
   u1.get("called") == 1 and u1.get("done") == 2, u1)
ck("the dial that made a follow-up, a dial two hours early, and somebody else's dial do not complete it",
   u1.get("overdue") == 3, u1)
ck("a follow-up whose time has not come is still to come", u1.get("upcoming") == 1, u1)
ck("due = done + overdue + still to come", u1.get("due") == u1.get("done", 0) + u1.get("overdue", 0) + u1.get("upcoming", 0), u1)
ck("matched through the contact as well as the deal", u2.get("called") == 1 and u2.get("due") == 1, u2)
ck("one COQL read per 50 owners, 2,000 rows a page, bounded by the owners and the window",
   len(SENT) == 1 and "Owner.id in ('U1','U2')" in SENT[0] and "limit 2000 offset 0" in SENT[0]
   and "Call_Start_Time between" in SENT[0], SENT)
SENT.clear()
Z.requests.post = fake_post
try:
    z.follow_up_breakdown(T % "00:00", T % "23:59", ["X%d" % i for i in range(60)], now=now)
finally:
    Z.requests.post = real_post
ck("60 owners: two reads (COQL `in` takes 50 at most)", len(SENT) == 2, len(SENT))
ck("no owners, no read", z.follow_up_breakdown(T % "00:00", T % "23:59", []) == {}, "empty")

# ---- the server side: names -> CRM users, cached, never zeros while reading ----
real = (A._zoho, A._FU_ASYNC, A._v5_allowed, A.COORD_LINK_SECRET)


class FakeZoho:
    calls = 0

    def list_users(self):
        return {"U1": "Maia Pasifae Palma", "U2": "Judith"}

    def follow_up_breakdown(self, start_iso, end_iso, ids, now=None):
        FakeZoho.calls += 1
        self.asked = (start_iso, end_iso, list(ids))
        return {"U1": {"due": 5, "done": 2, "marked": 2, "called": 0, "overdue": 1, "upcoming": 2, "cancelled": 9}}


try:
    fz = FakeZoho()
    A._zoho = fz
    A._FU_ASYNC = False
    A._fu_cache.clear(); A._fu_inflight.clear()
    by, meta = A._followups_for(["Maia Pasifae Palma", "Judith Merlo", "Cristina Cots"], T % "00:00", T % "23:59")
    ck("matched by CRM user id, through the coordinators' aliases", by and by["Maia Pasifae Palma"]["done"] == 2
       and "Judith Merlo" in by and fz.asked[2] == ["U1", "U2"], (by, getattr(fz, "asked", None)))
    ck("a CRM user with no follow-ups reads 0, not unknown", by["Judith Merlo"]["due"] == 0, by)
    ck("a name with no CRM user is named, not zero", meta.get("not_crm_users") == ["Cristina Cots"]
       and "Cristina Cots" not in by, meta)
    A._followups_for(["Cristina Cots", "Judith Merlo", "Maia Pasifae Palma"], T % "00:00", T % "23:59")
    ck("the same names in any order are one cached read", FakeZoho.calls == 1, FakeZoho.calls)

    A._FU_ASYNC = True
    A._fu_cache.clear(); A._fu_inflight.clear()
    started = []
    real_thread = A.threading.Thread
    A.threading.Thread = lambda target=None, args=(), name=None, daemon=None: type("T", (), {"start": lambda s: started.append(name)})()
    try:
        by2, meta2 = A._followups_for(["Maia Pasifae Palma"], T % "00:00", T % "23:59")
    finally:
        A.threading.Thread = real_thread
    ck("cold: the read starts on a thread and the answer is 'loading', never a board of zeros",
       by2 is None and meta2.get("loading") and started == ["followups"], (by2, meta2, started))

    class Boom(FakeZoho):
        def follow_up_breakdown(self, *a, **k):
            raise RuntimeError("CRM COQL returned HTTP 500")
    A._zoho = Boom()
    A._FU_ASYNC = False
    A._fu_cache.clear(); A._fu_inflight.clear()
    by3, meta3 = A._followups_for(["Maia Pasifae Palma"], T % "00:00", T % "23:59")
    ck("a failed read is an error, not zeros, and is kept only a minute",
       by3 is None and "CRM read failed" in (meta3.get("error") or "")
       and A.time.time() - list(A._fu_cache.values())[0]["at"] >= A._FU_TTL - 61, meta3)

    # the route
    A._zoho = fz
    A._fu_cache.clear(); A._fu_inflight.clear()
    c = A.app.test_client()
    A._v5_allowed = lambda: False
    A.COORD_LINK_SECRET = ""
    r = c.get("/api/v5/followups?start=2026-10-19&tz=240")
    ck("signed out and no key: 401", r.status_code == 401, r.status_code)
    A.COORD_LINK_SECRET = "test-secret"
    r = c.get("/api/v5/followups?start=2026-10-19&tz=240&k=" + A._teams_token() + "&names=Maia%20Pasifae%20Palma|Somebody%20New")
    j = r.get_json() or {}
    ck("the /teams key opens it", r.status_code == 200, r.status_code)
    ck("the window is the local day", fz.asked[0] == "2026-10-19T00:00:00-04:00" and fz.asked[1] == "2026-10-19T23:59:59-04:00", fz.asked)
    names = set((j.get("by") or {}).keys())
    ck("the page's names plus both declared sales rosters (so /v5 and /teams share a read)",
       {"Maia Pasifae Palma", "Somebody New", "Gabriel Johnson", "Roman Gomez"} <= names, sorted(names))
    ck("counts only: no patient, deal or number in the answer",
       not re.search(r"Patient|Subject|What_Id|\(\d{3}\)", json.dumps(j)), json.dumps(j)[:300])
    A._v5_allowed = lambda: True
    ck("a bad date is refused", c.get("/api/v5/followups?start=19-10-2026").status_code == 400, "date")
finally:
    A._zoho, A._FU_ASYNC, A._v5_allowed, A.COORD_LINK_SECRET = real
    A._fu_cache.clear(); A._fu_inflight.clear()

# ---- the pages are wired ----
v5 = open(os.path.join(HERE, "templates", "scoreboard_v5.html"), encoding="utf-8").read()
tm = open(os.path.join(HERE, "templates", "teams.html"), encoding="utf-8").read()
ck("/v5 fetches the follow-ups after each report and paints both boards",
   "loadFu(q);" in v5 and "fuPaint();" in v5 and "[$('out'), $('exout')].forEach" in v5 and "/api/v5/followups?" in v5, "v5")
ck("/v5: a Follow-ups line in both panels", v5.count("cell2('Follow-ups'") == 1 and "['Follow-ups', '<span class=\"dfu\"" in v5, "panel")
ck("/v5: the texts line stays above the hairline", "bar.insertAdjacentHTML('beforebegin', html)" in v5, "order")
ck("/teams: both sales sections carry the bar, the panel line and a summary row",
   "TB.fuBar(fuOf(a.name))" in tm and "bar: function (a) { return TB.fuBar(fuOf(a.name)); }" in tm
   and "sumExtra:" in tm and ".concat(fuRow(" in tm and "loadFu(q, gen," in tm, "teams")
ck("/teams: another day's follow-ups are dropped when the window changes", "if (win !== FU_WIN) { FU = null; FU_WIN = win; }" in tm, "win")

# ---- the bar itself, run in node ----
node = shutil.which("node")
if node:
    tb = re.search(r"<script>(.*?)</script>", open(os.path.join(HERE, "templates", "_teamboard.html"), encoding="utf-8").read(), re.S).group(1)
    js = ("var window = {}; var document = {querySelectorAll: function(){return [];}, getElementById: function(){return null;}};\n" + tb +
          "\nvar TB = window.TB, o = {};\n"
          "o.half = TB.fuBar({due: 6, done: 2, called: 1, overdue: 2, upcoming: 2, cancelled: 5});\n"
          "o.none = TB.fuBar({due: 3, done: 0, overdue: 3, upcoming: 0});\n"
          "o.all = TB.fuBar({due: 4, done: 4, overdue: 0, upcoming: 0});\n"
          "o.later = TB.fuBar({due: 5, done: 0, overdue: 0, upcoming: 5});\n"
          "o.nobody = TB.fuBar(null);\n"
          "o.line = TB.fuLine({due: 6, done: 2, called: 1, overdue: 2, upcoming: 2});\n"
          "o.zero = TB.fuLine({due: 0, done: 0, overdue: 0, upcoming: 0});\n"
          "o.notcrm = TB.fuLine(undefined, 'not_crm');\n"
          "o.reading = TB.fuLine(undefined, 'loading');\n"
          "o.sum = TB.fuSum({A: {done: 2, overdue: 2, upcoming: 2, called: 1}, B: {done: 1, overdue: 0, upcoming: 3}}, ['A', 'B', 'C']);\n"
          "o.sumNone = TB.fuSum({}, ['A']);\n"
          "var m = TB.ringexModel({ranked: [{name: 'Roman Gomez', talk: 60, attempts: 3, over_3: 0}], totals: {}, meta: {books: {}}},"
          " {bar: function (a) { return '<BAR ' + a.name + '>'; }, sumExtra: function () { return [['Follow-ups', 'x']]; }, notesExtra: ['FU']});\n"
          "o.press = m.rows[0].press; o.sumRows = m.sum.map(function (s) { return s[0]; }); o.notes = m.notes[m.notes.length - 1];\n"
          "console.log(JSON.stringify(o));")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    tmp.write(js); tmp.close()
    r = subprocess.run([node, tmp.name], capture_output=True, text=True)
    os.unlink(tmp.name)
    try:
        o = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        o = {}
        errors.append("node run failed: " + r.stderr[:300])
    if o:
        ck("the bar is done over the follow-ups whose time has come: 2 of 4 = 50%, amber",
           'aria-label="50% done"' in o["half"] and "width:50%" in o["half"] and "rgb(217,119,6)" in o["half"]
           and 'class="prog fu"' in o["half"], o["half"])
        ck("its tooltip says what is in it: called-not-marked, still to come, cancelled not counted",
           "1 of them called but not marked complete" in o["half"] and "2 still to come" in o["half"]
           and "5 cancelled by Zoho, not counted" in o["half"], o["half"])
        ck("none done yet still SHOWS: a short red sliver", "width:4%" in o["none"] and "rgb(176,58,49)" in o["none"], o["none"])
        ck("all done is full green", "width:100%" in o["all"] and "rgb(22,163,74)" in o["all"], o["all"])
        ck("nothing come due yet, or not known: no bar", o["later"] == "" and o["nobody"] == "", (o["later"], o["nobody"]))
        ck("the panel line: done of come-due, overdue in red, still to come, called-not-marked",
           o["line"].startswith("2 of 4 done") and '<span class="hot">2 overdue</span>' in o["line"]
           and "2 still to come" in o["line"] and "1 called, not marked" in o["line"], o["line"])
        ck("none due reads 0, an unmatched name says so, a read in flight says reading",
           o["zero"].startswith("0 ") and "not a CRM user" in o["notcrm"] and "reading Zoho" in o["reading"],
           (o["zero"], o["notcrm"], o["reading"]))
        ck("the team line adds up the known reps only", "<b>3</b> of 5 done" in o["sum"]
           and '<b class="hot">2</b> overdue' in o["sum"] and "<b>5</b> still to come" in o["sum"] and o["sumNone"] == "",
           (o["sum"], o["sumNone"]))
        ck("the training board takes the bar LAST under the name, a summary row and a note",
           o["press"].endswith("<BAR Roman Gomez>") and o["sumRows"][-1] == "Follow-ups" and o["notes"] == "FU", o)
else:
    errors.append("node is not installed -- the renderer could not be run, which is a failure, not a pass")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
