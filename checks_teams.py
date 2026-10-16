"""The RingEX reps' board under the sales roster, and /teams -- every team on
one page in one design.

Danny, 2026-10-16: "Add a new board below the current roster for the 9 new
RingEX reps on the sales page. Then create a page for me that has all the teams
on one combined page all the same design across each team type".
Run: ./.venv/bin/python -B checks_teams.py   (tally LAST)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import billing_report as br  # noqa: E402
import app as A  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


# ---- the engine: >3m and >10m exist on every RingEX day bucket ----
ck("the call-length marks include 3 and 10 minutes", 180 in br.CONV_MARKS and 600 in br.CONV_MARKS, br.CONV_MARKS)
b = br._blank_bucket()
br._fold(b, {"direction": "Outbound", "result": "Call connected", "duration": 200})
br._fold(b, {"direction": "Outbound", "result": "Call connected", "duration": 700})
br._fold(b, {"direction": "Outbound", "result": "No Answer", "duration": 900})
ck("a 200 s and a 700 s call: two over 3m, one over 10m; ring time never counts",
   b["over_180s"] == 2 and b["over_600s"] == 1 and b["dials"] == 3 and b["connected"] == 2, b)

# ---- the team ----
NINE = ["Annyta Santiago", "Ramon Gallardo", "Roberto Castillo", "Roman Gomez", "Jabes Perdomo",
        "Howard Salmon", "Javier Perez", "Seydi Medrano", "Ambar Luciano"]
ck("the training class is the October RingEX reps by extension, Roberto Castillo off the board (let go 10-15)",
   [s["name"] for s in A._TEAM_ROSTERS["ringex"]] == [n for n in NINE if n != "Roberto Castillo"]
   and all(s["ext_id"] for s in A._TEAM_ROSTERS["ringex"]), [s["name"] for s in A._TEAM_ROSTERS["ringex"]])
ck("...and shown as the Sales Training Class", A.TEAM_LABELS["ringex"] == "Sales Training Class", A.TEAM_LABELS["ringex"])
ck("...read from RingEX on demand, NOT warmed: no 90-day backfill competing with billing's live day",
   "ringex" not in A._TEAM_SOURCES and all(s.get("always_rank") for s in A._TEAM_ROSTERS["ringex"]), A._TEAM_SOURCES.get("ringex"))
ck("...texts not read twice (/v5 reads these seats already)", A._TEAM_SMS["ringex"] is False, A._TEAM_SMS)
ck("...with a board of its own", A._TEAM_PATHS["ringex"] == "sales-ringex"
   and any(r.rule == "/sales-ringex" for r in A.app.url_map.iter_rules()), A._TEAM_PATHS)
ck("the team key resolves (it used to fall back to billing for an unknown name)", A._team_key("ringex") == "ringex",
   A._team_key("ringex"))
_src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
ck("the new reps are not excluded from the sales floor as 'another team'",
   'if team in ("sales", "ringex"):     # both are the sales floor' in _src, "exclusion")
ck("the SMS seats for sales still read all nine lines (the STOP scan keeps Roberto's)",
   [s["name"] for s in A._SMS_EXTRA_SEATS["sales"]] == NINE, A._SMS_EXTRA_SEATS["sales"])

# ---- the rows: v6 days -> the /v5 row shape ----
day = lambda **k: dict({"calls": 0, "dials": 0, "inbound": 0, "inbound_answered": 0, "connected": 0,
                        "talk_seconds": 0, "over_180s": 0, "over_600s": 0, "longest_seconds": 0}, **k)
REPORT = {"as_of": "3:00 PM", "meta": {"complete": True}, "warnings": [], "ranked": [
    {"name": "Roman Gomez", "ext": "146", "days": [day(calls=160, dials=160, connected=40, talk_seconds=5640,
                                                       over_180s=8, over_600s=1, longest_seconds=900)],
     "day": {"focus": {"first": "10:31 AM", "late_minutes": 31}}, "missed_summary": {"missed": 1, "open": 1},
     "missed_calls": [{"at": "1:00 PM", "number": "(305) 555-0100", "resolved": False}]},
    {"name": "Jabes Perdomo", "ext": "141", "days": [day(calls=153, dials=150, inbound=3, inbound_answered=2,
                                                        connected=60, talk_seconds=8520, over_180s=6)],
     "day": {"focus": {"first": "10:10 AM"}}, "missed_summary": {}},
    {"name": "Howard Salmon", "ext": "156", "days": [day()], "day": {}, "missed_summary": {}},
]}
BOOKS = {A._norm_name("Jabes Perdomo"): {"quotes_sent": 1, "retainers_paid": 0, "retainers_sent": 1,
                                         "quotes_invoiced": 0, "paid_amount": 0}}
rows = A._ringex_rows(REPORT, BOOKS)
ck("rows are ranked by talk", [r["name"] for r in rows] == ["Jabes Perdomo", "Roman Gomez", "Howard Salmon"],
   [r["name"] for r in rows])
j = rows[0]
ck("talk in seconds, dials, >3m, in/out, connected -- the /v5 field names",
   (j["talk"], j["attempts"], j["outbound"], j["over_3"], j["inbound"], j["inbound_answered"], j["connected"])
   == (8520, 150, 150, 6, 3, 2, 60), j)
ck("Books' quotes and retainers by name", j["books"]["quotes_sent"] == 1 and j["books"]["retainers_sent"] == 1, j["books"])
r2 = rows[1]
ck("first call, minutes late, missed calls and the open count carry through",
   r2["first"] == "10:31 AM" and r2["late_minutes"] == 31 and r2["missed_open"] == 1 and len(r2["missed_calls"]) == 1, r2)
ck("someone with no calls is still a row, at zero, with empty Books",
   rows[2]["attempts"] == 0 and rows[2]["books"]["quotes_sent"] == 0, rows[2])

# ---- the API: gated, totals and floor, numbers hidden from the share link ----
c = A.app.test_client()
real = (A._v5_allowed, A._v6_cached_report, A._v5_books, A.GOOGLE_CLIENT_ID)
try:
    A._v5_allowed = lambda: False
    ck("/api/v5/ringex is closed without the sales board's gate", c.get("/api/v5/ringex").status_code == 401, "401")
    # No RingCentral credentials in a checkout: say "configured" for this block only.
    _cfg = type(A._ringcx).__dict__.get("configured")
    type(A._ringcx).configured = property(lambda self: True)
    A._v5_allowed = lambda: True
    seen = {}
    A._v6_cached_report = lambda team, s, e, tz: (seen.update(team=team, s=s, e=e, tz=tz), REPORT)[1]
    A._v5_books = lambda s, e: (BOOKS, {"cached": True})
    A.GOOGLE_CLIENT_ID = ""
    r = c.get("/api/v5/ringex?start=2026-10-16&tz=240")
    d = r.get_json()
    ck("it reads the ringex team for the window asked", r.status_code == 200 and seen == {"team": "ringex", "s": "2026-10-16",
                                                                                        "e": "2026-10-16", "tz": 240}, (r.status_code, seen))
    ck("totals add up across the nine", d["totals"]["talk"] == 14160 and d["totals"]["attempts"] == 310
       and d["totals"]["over_3"] == 14 and d["totals"]["missed_open"] == 1, d["totals"])
    ck("the floor is the median of those who worked (a zero day is not a person)",
       d["floor"]["talk"] == (8520 + 5640) / 2 and d["floor"]["attempts"] == 155, d["floor"])
    ck("signed in (or no Google gate): the missed numbers are there", d["ranked"][1]["missed_calls"], d["ranked"][1])
    A.GOOGLE_CLIENT_ID = "x"
    with c.session_transaction() as s:
        s.clear()
    d2 = c.get("/api/v5/ringex?start=2026-10-16&tz=240").get_json()
    ck("the share link gets counts, never a patient's number",
       all(not r["missed_calls"] for r in d2["ranked"]) and d2["ranked"][1]["missed_open"] == 1, d2["ranked"][1])
finally:
    A._v5_allowed, A._v6_cached_report, A._v5_books, A.GOOGLE_CLIENT_ID = real
    if "_cfg" in globals() and _cfg is not None:
        type(A._ringcx).configured = _cfg

# ---- /teams: signed in only, four sections, the sales board's own styles ----
real6 = A._v6_allowed
try:
    A._v6_allowed = lambda: False
    r = c.get("/teams")
    ck("/teams sends a signed-out visitor to sign in", r.status_code == 302 and "/login" in r.headers.get("Location", ""),
       (r.status_code, r.headers.get("Location")))
    A._v6_allowed = lambda: True
    html = c.get("/teams").get_data(as_text=True)
finally:
    A._v6_allowed = real6
ck("/teams: sales first, then the training class, coordinators, billing", [m for m in re.findall(r'id="t-([a-z]+)"', html)] ==
   ["sales", "ringex", "surgical", "billing"], re.findall(r'id="t-([a-z]+)"', html))
ck("...titled like the dashboard's boards, in Title Case", all(t in html for t in ('<h2 class="bt">Sales Floor</h2>',
   '<h2 class="bt">Sales Training Class</h2>', '<h2 class="bt">Surgical Coordinators</h2>', '<h2 class="bt">Billing</h2>')), "titles")
ck("...each team carries its own notes on delays and meanings", html.count("'<b>Delay.</b>") >= 3
   and "TRAINING_NOTES" in html and "15 to 20 minutes" in html and ("2\\u00bd minutes" in html or "2\u00bd minutes" in html), "notes")
ck("coordinators and billing keep their own board's tracking: graded against the KPI target, paced, with the progress bar",
   "grade: true" in html and "function v6Grades(a)" in html and "pace.grades.talk_minutes" in html
   and "function progressBar(a)" in html and ": press(ms.open)) + progressBar(a);" in html
   and "['Target', '<b>' + one('talk_minutes')" in html and "soFar(a, 'talk_minutes', 'm')" in html, "kpi")
ck("...and every figure reads as on their own board: minutes, In / Out, vs expected, planner+tasks done, planner overdue, the priorities line, their order",
   "talkText: fmin(show.talk_minutes)" in html and "' in \\u00b7 '" in html and "row.t3Lbl = ' vs exp';" in html
   and "pDone + '<i style" in html and "q.calls_overdue" in html and "' to schedule \\u00b7 '" in html and "keepOrder: true" in html, "match")
ck("billing's total is the board's own (what billers logged), never a sum of the rows whose calls are read",
   "T.coll = j.live && C.today != null ? C.today : (C.total || 0);" in html
   and "(j.unknown || []).concat(j.silent || [], j.stalled || [])" in html, "billing total")
ck("...and a biller not read yet on the phone still shows her collected amount",
   "(r.a ? res(r.a, 'q', m.aLbl || m.colA) : dash('q'))" in html, "unread amount")
ck("...the Sales Floor opens on a phone; the others start folded", "openOnPhone: true" in html
   and "return (FOLD[k] = phone && !m.openOnPhone);" in html, "fold")
ck("...each from its own board's API", all(u in html for u in ("/api/v6/report?team=surgical&", "/api/v6/report?team=billing&",
                                                               "/api/v5/report?", "/api/v5/ringex?")), "urls")
style = open(os.path.join(HERE, "templates", "_board_style.html"), encoding="utf-8").read()
ck("...in the sales board's design: the same stylesheet, word for word", style in html, "style")
ck("...and the same row renderer as the board under the sales roster", "window.TB = (function" in html, "TB")

# ---- the /teams key: no sign-in, no password, but not open; numbers cut to counts ----
realk = (A.COORD_LINK_SECRET, A.GOOGLE_CLIENT_ID, A._api_v6_report_impl, A._v6_cached_report, A._v5_books)
try:
    A.COORD_LINK_SECRET = "test-secret"
    A.GOOGLE_CLIENT_ID = "x"
    tok = A._teams_token()
    ck("the key is derived from the secret, never stored in code, and differs from every coordinator's",
       len(tok) == 24 and tok not in _src and all(tok != A._coord_token(A._coord_slug(x["name"]))
                                                  for x in A._TEAM_ROSTERS["surgical"]), tok)
    c2 = A.app.test_client()
    ck("/teams opens on the key with no sign-in", c2.get("/teams?k=" + tok).status_code == 200, "200")
    ck("...and not on a wrong key", c2.get("/teams?k=nope").status_code == 302, "302")
    ck("the link itself is only printed to a signed-in viewer", c2.get("/api/teams/link").status_code == 401, "401")
    with c2.session_transaction() as sess:
        sess["user"] = {"email": "danny@example.com"}
    lk = c2.get("/api/teams/link").get_json()
    ck("...which gets the full URL", lk.get("url", "").endswith("/teams?k=" + tok), lk)
    with c2.session_transaction() as sess:
        sess.clear()
    A._api_v6_report_impl = lambda: A.jsonify({"ranked": [{"name": "Ariel Ramirez", "missed_calls": [{"number": "(305) 555-0100"}],
                                                           "missed_summary": {"open": 1}}], "meta": {}})
    r = c2.get("/api/v6/report?team=surgical&k=" + tok)
    body = r.get_json()
    ck("the key opens the coordinators' report with the missed numbers cut, the count kept",
       r.status_code == 200 and body["ranked"][0]["missed_calls"] == [] and body["ranked"][0]["missed_summary"]["open"] == 1, body)
    ck("the key opens billing's report too", c2.get("/api/v6/report?team=billing&k=" + tok).status_code == 200, "billing")
    ck("...but no other board", c2.get("/api/v6/report?team=scheduling&k=" + tok).status_code == 401, "scheduling")
    ck("no key, no report", c2.get("/api/v6/report?team=surgical").status_code == 401, "nokey")
    A._v6_cached_report = lambda team, s, e, tz: REPORT
    A._v5_books = lambda s, e: (BOOKS, {})
    _cfg2 = type(A._ringcx).__dict__.get("configured")
    type(A._ringcx).configured = property(lambda self: True)
    rx = c2.get("/api/v5/ringex?start=2026-10-16&k=" + tok)
    ck("the key opens the RingEX reps' rows, numbers cut", rx.status_code == 200 and
       all(not x["missed_calls"] for x in rx.get_json()["ranked"]), rx.status_code)
    ck("/api/v5/report accepts the key", "if not (_v5_allowed() or _teams_token_ok()):" in _src, "v5 gate")
finally:
    A.COORD_LINK_SECRET, A.GOOGLE_CLIENT_ID, A._api_v6_report_impl, A._v6_cached_report, A._v5_books = realk
    if "_cfg2" in globals() and _cfg2 is not None:
        type(A._ringcx).configured = _cfg2
A.COORD_LINK_SECRET = ""
ck("no secret, no key: /teams?k= fails closed", A._teams_token() == "" and c.get("/teams?k=").status_code in (200, 302), "closed")
ck("the page passes its key to every section", "if (K) q.set('k', K);" in open(os.path.join(HERE, "templates", "teams.html"), encoding="utf-8").read(), "K")

# ---- RingEX pacing: a request never fetches the nine reps' today ----
calls = []
real_fd = A._v6_fetch_day
try:
    A._v6_fetch_day = lambda eid, day, tz: (calls.append(eid), ([], True))[1]
    # through the real roster loader, which rebuilds every seat field by field
    seat = dict(A._billing_roster("ringex")[0][0])
    ck("the roster loader keeps the training class's cache-only flag (it dropped it)", seat.get("cache_only_today") is True, seat)
    A._v6_today_cache.pop((seat["ext_id"], "2099-01-01"), None)
    got, stats = A._v6_fetch_ringex([seat], ["2099-01-01"], "2099-01-01", 240)
    ck("a RingEX rep with no fresh cache is 'not read yet', and the request made no RingEX call",
       calls == [] and got[seat["name"]]["complete"] is False and stats["missing"] == 1, (calls, stats))
    A._v6_today_cache[(seat["ext_id"], "2099-01-01")] = {"at": __import__("time").time() - 600, "rows": [{"x": 1}]}
    got, stats = A._v6_fetch_ringex([seat], ["2099-01-01"], "2099-01-01", 240)
    ck("...a 10-minute-old cache from their own loop is served (the board's own refresh is 5)",
       calls == [] and got[seat["name"]]["complete"] is True and stats["cached"] == 1, stats)
    bill = dict(A._TEAM_ROSTERS["billing"][0])
    A._v6_today_cache.pop((bill["ext_id"], "2099-01-01"), None)
    A._v6_fetch_ringex([bill], ["2099-01-01"], "2099-01-01", 240)
    ck("...while a billing seat still fetches in the request as before", calls == [bill["ext_id"]], calls)
finally:
    A._v6_fetch_day = real_fd
    A._v6_today_cache.pop((A._TEAM_ROSTERS["ringex"][0]["ext_id"], "2099-01-01"), None)
# ---- a refused re-read keeps today's last reading (Danny's 6:32 PM phone screenshot) ----
import time as _t
_rf = A._v6_fetch_day
try:
    A._v6_fetch_day = lambda eid, day, tz: ([], False)          # RingEX refuses
    bseat = dict(A._TEAM_ROSTERS["billing"][0]); D0 = "2099-01-03"
    A._v6_today_cache[(bseat["ext_id"], D0)] = {"at": _t.time() - 600, "rows": [{"id": "x"}]}
    got, st = A._v6_fetch_ringex([bseat], [D0], D0, 240)
    ck("a biller whose re-read is refused keeps her 10-minute-old reading, marked by its age -- not 'not read yet'",
       got[bseat["name"]]["complete"] is True and got[bseat["name"]]["rows"] == [{"id": "x"}]
       and st["stale_seats"] == {bseat["name"]: 10} and st["missing"] == 0, st)
    A._v6_today_cache[(bseat["ext_id"], D0)] = {"at": _t.time() - 4000, "rows": [{"id": "x"}]}
    got, st = A._v6_fetch_ringex([bseat], [D0], D0, 240)
    ck("...but a reading over an hour old is not passed off as today's: not read yet", got[bseat["name"]]["complete"] is False
       and st["missing"] == 1 and not st["stale_seats"], st)
    A._v6_fetch_day = lambda eid, day, tz: ([{"id": "fresh"}], True)
    got, st = A._v6_fetch_ringex([bseat], [D0], D0, 240)
    ck("...and a fresh read, when RingEX answers, always wins", got[bseat["name"]]["rows"] == [{"id": "fresh"}] and not st["stale_seats"], st)
    tseat = dict(A._billing_roster("ringex")[0][0])
    A._v6_today_cache[(tseat["ext_id"], D0)] = {"at": _t.time() - 1800, "rows": [{"id": "t"}]}
    got, st = A._v6_fetch_ringex([tseat], [D0], D0, 240)
    ck("a training-class rep's last reading stands however old, marked (their loop is last in line for RingEX)",
       got[tseat["name"]]["complete"] is True and st["stale_seats"] == {tseat["name"]: 30}, st)
finally:
    A._v6_fetch_day = _rf
    for _k in [k for k in A._v6_today_cache if k[1] == "2099-01-03"]:
        A._v6_today_cache.pop(_k, None)
# ---- today's reading survives a restart ----
import tempfile as _tf, pathlib as _pl
_realdir = A.V6_TODAY_DIR
try:
    A.V6_TODAY_DIR = _pl.Path(_tf.mkdtemp())
    A._v6_today_put(123, "2099-01-04", [{"id": "r"}])
    A._v6_today_cache.pop((123, "2099-01-04"), None)          # a restart empties memory
    _c = A._v6_today_get(123, "2099-01-04")
    ck("today's reading is written to disk and read back after a restart (not as a finished day)",
       _c and _c["rows"] == [{"id": "r"}] and A._v6_load_day(123, "2099-01-04") is None, _c)
finally:
    A.V6_TODAY_DIR = _realdir
    A._v6_today_cache.pop((123, "2099-01-04"), None)
ck("'2 of 4 on target', not '2/4'", "[T.onT + ' of ' + T.people, 'on target']" in html
   and "'<b>' + T.onT + '</b> of ' + T.people + ' on target'" in html and "T.onT + '/' + T.people" not in html, "of")

ck("the report names the seats shown from an earlier reading and how old", '"kind": "stale_seat"' in _src
   and 'a["stale_minutes"] = stale_seats[a["name"]]' in _src, "warning")
ck("/teams shows 'read N min ago' under such a name",
   "'<span class=\"unr\">read ' + a.stale_minutes + ' min ago</span>'" in html, "teams")

ck("their own slow loop is a worker loop, LAST in line: one seat a minute, a 70 s back-off on a refusal",
   any(n == "loop:ringex" for n, _ in A._WORKER_LOOPS) and A._TC_PAUSE == 60.0 and A._RINGEX_REFRESH_AFTER == 300.0 and A._TC_REFRESH_AFTER == 420.0 and "time.sleep(70)" in _src, "loop")
_today = "2099-01-02"
for _s in A._billing_roster("billing")[0]:
    A._v6_today_cache.pop((_s["ext_id"], _today), None)
ck("...and it yields while any billing seat's day is not fresh", A._ringex_should_yield(_today) is True, "yield")
for _s in A._billing_roster("billing")[0]:
    A._v6_today_cache[(_s["ext_id"], _today)] = {"at": __import__("time").time(), "rows": []}
_rl = A._ringcx.rate_limited
A._ringcx.rate_limited = lambda: False
ck("...and goes when billing is fresh and RingEX is not refusing", A._ringex_should_yield(_today) is False, "go")
A._ringcx.rate_limited = lambda: True
ck("...and waits out a RingEX cooldown", A._ringex_should_yield(_today) is True, "cooldown")
A._ringcx.rate_limited = _rl
for _s in A._billing_roster("billing")[0]:
    A._v6_today_cache.pop((_s["ext_id"], _today), None)

# ---- Johana: off Friday and Saturday (Danny, 2026-10-16) ----
from datetime import datetime as _D
ck("Johana Duron is off Friday and Saturday on every board (the shift table they all read)",
   A.SHIFTS["Johana Duron"]["off"] == ["fri", "sat"]
   and A._shift_phase("Johana Duron", _D(2026, 10, 16, 12, 0)) == ("off", "Off Today")
   and A._shift_phase("Johana Duron", _D(2026, 10, 17, 12, 0)) == ("off", "Off Today")
   and A._shift_phase("Johana Duron", _D(2026, 10, 15, 12, 0)) is None, A.SHIFTS["Johana Duron"])
ck("/teams says 'off today' for a scheduled day off with no calls, not 'no calls yet today'",
   "r.quietLabel = 'off today'" in html and "esc(r.quietLabel || 'no calls yet today')" in html, "off today")

# ---- /v5: the board under the roster ----
with A.app.test_request_context("/v5"):
    v5 = A.render_template("scoreboard_v5.html", share_mode=False, share_token="")
ck("/v5 still has its own styles (moved to the partial, not lost)", style in v5 and v5.count("<style>") == 2, v5.count("<style>"))
ck("/v5 has the RingEX board under the roster", v5.index('id="out"') < v5.index('id="exout"') and "loadEx(q);" in v5
   and "/api/v5/ringex?" in v5, "exout")
ck("texts and the missed/unread line paint on both boards", "[out, $('exout')].forEach" in v5, "smsPaint")

# ---- the renderer itself, run in node ----
node = shutil.which("node")
if node:
    tb = re.search(r"<script>(.*?)</script>", open(os.path.join(HERE, "templates", "_teamboard.html"), encoding="utf-8").read(), re.S).group(1)
    model_rows = [{"name": "Jabes Perdomo", "talk": 8520, "attempts": 150, "over_3": 6, "over_10": 0, "inbound": 3,
                   "inbound_answered": 2, "connected": 60, "longest": 900, "first": "10:10 AM", "missed_open": 0,
                   "missed_calls": [], "books": {"quotes_sent": 1, "retainers_paid": 0}},
                  {"name": "Roman <b>Gomez</b>", "talk": 5640, "attempts": 160, "over_3": 8, "over_10": 1, "inbound": 0,
                   "inbound_answered": 0, "connected": 40, "longest": 600, "first": "10:31 AM", "missed_open": 1,
                   "missed_calls": [{"at": "1:00 PM", "number": "(305) 555-0100", "resolved": False}],
                   "books": {"quotes_sent": 0, "retainers_paid": 0}},
                  {"name": "Howard Salmon", "talk": 0, "attempts": 0, "over_3": 0, "over_10": 0, "inbound": 0,
                   "inbound_answered": 0, "connected": 0, "longest": 0, "first": None, "missed_open": 0,
                   "missed_calls": [], "complete": False, "books": {}}]
    js = ("var window = {}; var document = {querySelectorAll: function(){return [];}, getElementById: function(){return null;}};\n" + tb +
          "\nvar el = {innerHTML: '', cls: {}, classList: {add: function(c){el.cls[c]=1;}, toggle: function(c, on){if(on) el.cls[c]=1; else delete el.cls[c];}," +
          " contains: function(c){return !!el.cls[c];}}, querySelectorAll: function(){return [];}, querySelector: function(){return null;}};\n"
          "var d = " + json.dumps({"ranked": model_rows, "totals": {"talk": 14160, "attempts": 310, "over_3": 14, "over_10": 1,
                                                                    "inbound": 3, "inbound_answered": 2, "connected": 100, "missed_open": 1},
                                   "meta": {"books": {}}}) + ";\n"
          "window.TB.render(el, window.TB.ringexModel(d, {key: 'rx', label: 'New reps'}));\n"
          "console.log(el.innerHTML); console.error(JSON.stringify(el.cls));")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    tmp.write(js); tmp.close()
    out = subprocess.run([node, tmp.name], capture_output=True, text=True)
    h = out.stdout
    ck("the renderer runs", out.returncode == 0 and h.strip(), out.stderr[:300])
    ck("rows rank by talk, with the sales columns: Talk, Dials, >3m, Quotes, Paid",
       h.index("Jabes Perdomo") < h.index("Roman") and all(x in h for x in ('class="a-tk">Talk', 'class="a-d">Dials', '&gt;3m',
                                                                           '>Quotes<', '>Paid<')), h[:400])
    ck("talk, dials and >3m carry an arrow against the median; quotes and paid do not",
       h.count('class="mk"') == 6 and '<span class="q"' in h and '<span class="rt zero"' in h, h.count('class="mk"'))
    ck("a seat RingEX has not been read for says 'not read yet', last, with no arrows and no zeros",
       "not read yet" in h and h.index("Howard Salmon") > h.index("Roman") and h.count('class="mk"') == 6, "unread")
    ck("a name is escaped, never HTML", "Roman &lt;b&gt;Gomez&lt;/b&gt;" in h and "Roman <b>Gomez" not in h, "esc")
    ck("a missed call not called back shows under the name and in the panel with a tap-to-call",
       '<b class="hot">1</b> missed' in h and 'href="tel:3055550100"' in h, "missed")
    ck("the summary says the team's totals, each number glued to its word",
       "<b>3:56:00</b>\u00a0talk" in h and "<b>310</b>\u00a0dials" in h, "sum")
    ck("the title folds the team; a folded team keeps a one-line headline",
       'class="bt tb-tog" role="button"' in h and ('<div class="tb-stats">' in h or '<p class="tb-hl">' in h) and '<div class="tb-body">' in h, "fold")
    ck("folded, a team shows four figures (value over label), and its board link sits on the title row",
       h.count('<div><b') >= 4 and '<span>talk</span>' in h and 'class="tb-head"' in h, "stats")
    ck("on a desktop (no phone media match) a team starts open", '"tb-sec":1' in out.stderr and "collapsed" not in out.stderr, out.stderr[-120:])
    ck("the section's notes sit at its foot, folded", '<details class="notes tb-notes"><summary>Notes</summary>' in h
       and "<b>Delay.</b>" in h and "<b>Columns.</b>" in h and "<b>Marks.</b>" in h, "notes")
    os.unlink(tmp.name)
    # graded rows (coordinators, billing): the mark follows the KPI band, not the median
    gm = ("var window = {}; var document = {querySelectorAll: function(){return [];}, getElementById: function(){return null;}};\n" + tb +
          "\nvar el = {innerHTML: '', classList: {add: function(){}, toggle: function(){}, contains: function(){return false;}}," +
          " querySelectorAll: function(){return [];}, querySelector: function(){return null;}};\n"
          "window.TB.render(el, {key: 'g', label: 'Surgical Coordinators', grade: true, colA: 'Calls done', colB: 'Overdue', t3Head: '&ge;2m', rows: ["
          "{name: 'Ahead', talk: 12000, dials: 60, over3: 20, g: {talk: 'stretch', dials: 'target', over3: 'floor'}, a: {html: '41'}, b: {html: '0', zero: true}},"
          "{name: 'Behind', talk: 600, dials: 5, over3: 0, g: {talk: 'below', dials: 'below', over3: 'below'}, a: {html: '2'}, b: {html: '8', hot: true}}]});\n"
          "console.log(el.innerHTML);")
    tmp2 = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    tmp2.write(gm); tmp2.close()
    o2 = subprocess.run([node, tmp2.name], capture_output=True, text=True)
    g = o2.stdout
    ahead, behind = g[g.index("Ahead"):g.index("Behind")], g[g.index("Behind"):]
    ck("graded: at/above target is up, between floor and target a quiet dot, under the floor red and down",
       o2.returncode == 0 and ahead.count('class="mk">\u25B2') == 2 and 'flr' in ahead and behind.count('class="mk">\u25BC') == 3,
       (o2.stderr[:200], ahead[:300]))
    ck("graded: a name under the floor on talk is red; the long-call header follows the board's threshold",
       '<div class="r low"' in g and g.index('<div class="r low"') > g.index("Ahead") and "&ge;2m" in g, g[:200])
    os.unlink(tmp2.name)
else:
    errors.append("node is not installed -- the renderer could not be run, which is a failure, not a pass")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
