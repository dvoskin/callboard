"""Missed calls: one row per caller, cleared by a callback on ANY company line,
and after-shift calls listed but not held against the day.

Danny, 2026-10-19, pasting Yareth Pavon's list: "Missed calls · 22 not called
back". It was 9 people; one of them had been called back by a sales rep and
answered on another line, which the billing board could not see.
Run: ./.venv/bin/python -B checks_missed_callers.py   (tally LAST)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import billing_report as br  # noqa: E402
import app as A  # noqa: E402
import ringcx_client as RC  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


def seat(rep, name="Yareth Pavon"):
    for k, v in rep.items():
        if isinstance(v, list):
            for a in v:
                if isinstance(a, dict) and a.get("name") == name and "missed_summary" in a:
                    return a
    raise AssertionError("no row for %s in %s" % (name, sorted(rep)))


def row(hhmm, direction, result, num, day="2026-10-19"):
    h, m = hhmm.split(":")
    return {"direction": direction, "result": result, "duration": 60 if result in ("Call connected", "Accepted") else 0,
            "start_time": "%sT%02d:%02d:00-04:00" % (day, int(h), int(m)), "number": num}


A_, B_, C_, D_, E_ = "3055550101", "3055550202", "3055550303", "3055550404", "3055550505"
yareth = [row(t, "Inbound", "Missed", A_) for t in ("10:19", "10:20", "10:20", "10:23", "10:42", "10:43")]
yareth += [row("19:00", "Inbound", "Missed", A_)]                                  # the same caller again, after the shift
yareth += [row(t, "Inbound", "Missed", B_) for t in ("16:59", "17:00", "17:01")]   # rang three times, nobody got back
yareth += [row("20:44", "Inbound", "Missed", C_)]                                  # after the shift, nobody got back
yareth += [row("11:11", "Inbound", "Missed", D_)]                                  # the same call answered on another line
yareth += [row("14:29", "Inbound", "Missed", E_), row("18:32", "Inbound", "Missed", E_)]
company = [
    {"start_time": "2026-10-19T12:43:00-04:00", "direction": "Outbound", "result": "Call connected", "number": A_, "who": "Maia Pasifae Palma"},
    {"start_time": "2026-10-19T19:04:00-04:00", "direction": "Inbound", "result": "Accepted", "number": A_, "who": "Billing Support"},
    {"start_time": "2026-10-19T11:11:02-04:00", "direction": "Inbound", "result": "Accepted", "number": D_, "who": "Kassandra Villanueva"},
    {"start_time": "2026-10-19T09:00:00-04:00", "direction": "Outbound", "result": "Call connected", "number": B_, "who": "Somebody"},  # BEFORE the misses
]
SHIFT = {"start": "10:00", "end": "19:00", "off": ["sat", "sun"]}
meta = {"Yareth Pavon": {"rows": yareth, "ext": "220", "ext_id": 1, "complete": True, "shift": SHIFT}}
W = {"start": "2026-10-19", "end": "2026-10-19"}

# ---- positive control: the board alone, as it was ----
alone = br.build_report(meta, tz_offset_minutes=-240, window=W)
ya = seat(alone)
ms0 = ya["missed_summary"]
ck("without the company feed the caller a sales rep called back still reads open (the bug, reproduced)",
   any(m["digits"] == A_ and not m["resolved"] for m in ya["missed_calls"]), ya["missed_calls"])
ck("...but already one row per caller: 14 calls, 5 people, not 14 rows",
   ms0["missed"] == 14 and ms0["callers"] == 5 and len(ya["missed_calls"]) == 5, ms0)

# ---- with the company feed ----
rep = br.build_report(meta, tz_offset_minutes=-240, window=W, company_rows=company)
ya = seat(rep)
L, ms = ya["missed_calls"], ya["missed_summary"]
rowsA = [m for m in L if m["digits"] == A_]
ck("a callback by anyone on any line clears the caller, and says who: six morning misses, one row, called back by the sales rep",
   any(m["resolved"] and m["count"] == 6 and m["back"]["by"] == "Maia Pasifae Palma" and m["back"]["how"] == "called back"
       and m["first_at"] == "10:19 AM" and m["at"] == "10:43 AM" for m in rowsA), rowsA)
ck("the same caller later, answered on another line four minutes after: its own row, cleared",
   any(m["resolved"] and m["count"] == 1 and m["at"] == "7:00 PM" and m["back"]["by"] == "Billing Support"
       and "answered" in m["back"]["how"] for m in rowsA) and len(rowsA) == 2, rowsA)
b = next(m for m in L if m["digits"] == B_)
ck("three ring-outs with nobody getting back: ONE row, three calls, open; an earlier call to them does not count",
   not b["resolved"] and b["count"] == 3 and b["first_at"] == "4:59 PM" and b["at"] == "5:01 PM" and not b["after_hours"], b)
c = next(m for m in L if m["digits"] == C_)
ck("a call after the shift ended is listed as after hours, still open", not c["resolved"] and c["after_hours"], c)
d = next(m for m in L if m["digits"] == D_)
ck("the same call answered on another line within seconds clears it", d["resolved"] and d["back"]["by"] == "Kassandra Villanueva", d)
e = next(m for m in L if m["digits"] == E_)
ck("two misses hours apart with nothing between are one caller to call back", not e["resolved"] and e["count"] == 2, e)
ck("the summary counts PEOPLE: 14 calls, 6 callers, 3 called back, 2 to call back in the shift, 1 after hours",
   ms == {"missed": 14, "callers": 6, "called_back": 3, "open": 2, "after_hours": 1}, ms)
ck("newest first", [m["at"] for m in L] == sorted([m["at"] for m in L], key=lambda x: datetime.strptime(x, "%I:%M %p"), reverse=True), [m["at"] for m in L])

off = br.build_report({"Yareth Pavon": dict(meta["Yareth Pavon"], shift=dict(SHIFT, off=["mon"]))},
                      tz_offset_minutes=-240, window=W)
ya_off = seat(off)
ck("on a day off every open caller is after hours, none held against the day",
   ya_off["missed_summary"]["open"] == 0 and ya_off["missed_summary"]["after_hours"] == ya_off["missed_summary"]["callers"], ya_off["missed_summary"])
noshift = br.build_report({"Yareth Pavon": dict(meta["Yareth Pavon"], shift=None)}, tz_offset_minutes=-240, window=W)
ya_ns = seat(noshift)
ck("no declared shift: nothing is after hours", ya_ns["missed_summary"]["after_hours"] == 0, ya_ns["missed_summary"])

# ---- the account slice reader ----
class _R:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._b, self.headers = status, body or {}, headers or {}
        self.ok = status < 400

    def json(self):
        return self._b


cli = RC.RingCXClient.__new__(RC.RingCXClient)
cli.server_url, cli.account_id = "https://example.invalid", "~"
cli._cool_until, cli._cool_until_groups, cli._cool_reason = 0.0, {}, None
cli._ensure_rc_token = lambda: "t"
cli._rc_headers = lambda: {}
recs = [{"id": "1", "startTime": "2026-10-19T16:43:00.000Z", "direction": "Outbound", "result": "Call connected",
         "from": {"name": "Maia Pasifae Palma", "phoneNumber": "+19545550000"}, "to": {"phoneNumber": "+13055550101"}},
        {"id": "2", "startTime": "2026-10-19T23:04:00.000Z", "direction": "Inbound", "result": "Accepted",
         "from": {"phoneNumber": "+13055550101", "name": "A PATIENT"}, "to": {"name": "Billing Support", "phoneNumber": "+19545557429"}}]
calls = []
real_get = RC.requests.get
RC.requests.get = lambda url, **k: (calls.append(k["params"]), _R(200, {"records": recs}))[1]
try:
    ev, mt = cli.fetch_account_call_slice(datetime(2026, 10, 19, 16, tzinfo=timezone.utc), datetime(2026, 10, 19, 17, tzinfo=timezone.utc))
finally:
    RC.requests.get = real_get
ck("the slice keeps only time, direction, result, the OUTSIDE number and OUR side's name",
   ev[0]["num"] == A_ and ev[0]["who"] == "Maia Pasifae Palma" and ev[1]["num"] == A_ and ev[1]["who"] == "Billing Support"
   and set(ev[0]) == {"id", "t", "dir", "res", "num", "who"} and mt["complete"], ev)
ck("one request for a short page, Simple view, the window as asked",
   len(calls) == 1 and calls[0]["view"] == "Simple" and calls[0]["dateFrom"] == "2026-10-19T16:00:00.000Z", calls)
RC.requests.get = lambda url, **k: _R(429, headers={"Retry-After": "40"})
try:
    ev2, mt2 = cli.fetch_account_call_slice(datetime(2026, 10, 19, 16, tzinfo=timezone.utc), datetime(2026, 10, 19, 17, tzinfo=timezone.utc))
finally:
    RC.requests.get = real_get
ck("a 429 opens the shared cooldown and the slice is NOT complete (the cursor must not move)",
   not mt2["complete"] and cli.rate_limited() and ev2 == [], (mt2, cli.cooldown_remaining()))
ck("in the cooldown nothing is requested", cli.fetch_account_call_slice(datetime(2026, 10, 19, 16, tzinfo=timezone.utc),
                                                                         datetime(2026, 10, 19, 17, tzinfo=timezone.utc))[1]["note"].startswith("shared"), "cooldown")

# ---- the feed: slices, cursor, disk ----
tmpdir = tempfile.mkdtemp()
real = (A._CO_DIR, A._ringcx.fetch_account_call_slice)
asked = []
try:
    A._CO_DIR = __import__("pathlib").Path(tmpdir) / "_company"
    A._co_days.clear()

    def fake(start, end, max_pages=4, timeout=30.0):
        asked.append((start, end))
        return [{"id": "x%d" % len(asked), "t": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "dir": "Outbound", "res": "Call connected",
                 "num": A_, "who": "Somebody"}, {"id": "nonum", "t": "", "dir": "Inbound", "res": "Missed", "num": "", "who": ""}], \
               {"complete": True, "pages": 1, "note": None}
    A._ringcx.fetch_account_call_slice = fake
    now = datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)          # 9:00 AM ET
    ck("a cold day starts at local midnight and reads 90 minutes", A._co_tick("2026-10-19", 240, now=now)
       and asked[0][0] == datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc) and asked[0][1] - asked[0][0] == timedelta(minutes=90), asked)
    while A._co_tick("2026-10-19", 240, now=now):
        if len(asked) > 20:
            break
    st = A._co_day("2026-10-19")
    ck("...slice by slice until it reaches now, each slice overlapping the last by 3 minutes",
       datetime.fromisoformat(st["cursor"]) == now and all(asked[i + 1][0] == asked[i][1] - timedelta(minutes=3) for i in range(len(asked) - 1)), asked)
    n = len(asked)
    ck("caught up: no request until 5 minutes have passed", not A._co_tick("2026-10-19", 240, now=now + timedelta(minutes=4)) and len(asked) == n, len(asked))
    ck("...then one", A._co_tick("2026-10-19", 240, now=now + timedelta(minutes=6)) and len(asked) == n + 1, len(asked))
    ck("a call with no outside number is not kept", "nonum" not in st["events"], list(st["events"])[:3])
    A._co_days.clear()
    rows_, feed = A._company_rows("2026-10-19", "2026-10-19")
    ck("the day is on disk: a restart reads it back without a request", feed["events"] == len(asked) and rows_[0]["number"] == A_
       and rows_[0]["who"] == "Somebody" and len(asked) == n + 1, feed)
    ck("a day with no feed is simply empty", A._company_rows("2026-09-01", "2026-09-01")[1]["days"] == 0, "empty")

    def fail(start, end, max_pages=4, timeout=30.0):
        asked.append((start, end))
        return [], {"complete": False, "pages": 0, "note": "HTTP 429"}
    A._ringcx.fetch_account_call_slice = fail
    before = A._co_day("2026-10-19")["cursor"]
    A._co_tick("2026-10-19", 240, now=now + timedelta(minutes=30))
    ck("a refused slice leaves the cursor where it was", A._co_day("2026-10-19")["cursor"] == before, before)
finally:
    A._CO_DIR, A._ringcx.fetch_account_call_slice = real
    A._co_days.clear()
    shutil.rmtree(tmpdir, ignore_errors=True)

src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
ck("the loop is registered, waits a minute after start, and stands down in a RingEX cooldown -- not on billing's stale test, true all night",
   '("loop:company", lambda: _company_loop())' in src and "def _company_loop():\n    time.sleep(60)" in src
   and "if _ringcx.rate_limited():\n                _co_state[\"yielded\"] += 1" in src, "loop")
ck("every v6 report is built with the feed and says how far it reaches",
   "sms_by_agent=sms_by_agent, company_rows=company_rows," in src and 'report["company_feed"] = company_feed' in src, "wired")

# ---- the pages ----
v6 = open(os.path.join(HERE, "templates", "scoreboard_v6.html"), encoding="utf-8").read()
ck("the coordinator's alert never sounds for an after-hours caller", "if (!m.resolved && !m.after_hours) ks.push('call:'" in v6, "alert")
ck("the dashboard panel says people to call back, called back, after hours, and the calls per caller",
   "' to call back</b> &middot; ' + ms.called_back + ' called back'" in v6 and "' calls since '" in v6 and "after hours" in v6, "panel")
node = shutil.which("node")
if node:
    tb = re.search(r"<script>(.*?)</script>", open(os.path.join(HERE, "templates", "_teamboard.html"), encoding="utf-8").read(), re.S).group(1)
    js = ("var window = {}; var document = {querySelectorAll: function(){return [];}, getElementById: function(){return null;}};\n" + tb +
          "\nvar el = {innerHTML: '', classList: {add: function(){}, toggle: function(){}, contains: function(){return false;}}," +
          " querySelectorAll: function(){return [];}, querySelector: function(){return null;}};\n"
          "window.TB.render(el, {key: 'm', label: 'Billing', colA: 'A', colB: 'B', rows: [{name: 'Yareth Pavon', talk: 100, dials: 3, over3: 0,"
          " missed: " + json.dumps(L) + ", missedOpen: " + str(ms["open"]) + "}]});\n"
          "console.log(el.innerHTML);")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    tmp.write(js); tmp.close()
    r = subprocess.run([node, tmp.name], capture_output=True, text=True)
    os.unlink(tmp.name)
    h = r.stdout
    ck("the list's header counts people and after-hours callers", "Missed calls · 2 people to call back · 1 after hours" in h, h[:400])
    ck("a caller who rang three times is one row: 'call back · 3 calls since 4:59 PM'",
       "call back · 3 calls since 4:59 PM" in h and h.count("(305) 555-0202") == 1, r.stderr[:300] or h[:300])
    ck("an after-hours caller is labelled, not red", '<span class="ml-s">after hours</span>' in h, "ah")
    ck("a cleared caller says who got back and how many calls it covered",
       "called back 12:43 PM · Maia Pasifae Palma · 6 calls since 10:19 AM" in h, "back")
else:
    errors.append("node is not installed -- the list could not be rendered, which is a failure, not a pass")

for e_ in errors:
    print("  FAIL", e_)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
