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
ck("the RingEX team is the nine October reps, by extension",
   [s["name"] for s in A._TEAM_ROSTERS["ringex"]] == NINE and all(s["ext_id"] for s in A._TEAM_ROSTERS["ringex"]),
   A._TEAM_ROSTERS["ringex"])
ck("...read from RingEX, every one ranked", A._TEAM_SOURCES["ringex"] == "ringex"
   and all(s.get("always_rank") for s in A._TEAM_ROSTERS["ringex"]), A._TEAM_SOURCES.get("ringex"))
ck("...texts not read twice (/v5 reads these seats already)", A._TEAM_SMS["ringex"] is False, A._TEAM_SMS)
ck("...with a board of its own", A._TEAM_PATHS["ringex"] == "sales-ringex"
   and any(r.rule == "/sales-ringex" for r in A.app.url_map.iter_rules()), A._TEAM_PATHS)
ck("the team key resolves (it used to fall back to billing for an unknown name)", A._team_key("ringex") == "ringex",
   A._team_key("ringex"))
_src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
ck("the new reps are not excluded from the sales floor as 'another team'",
   'if team in ("sales", "ringex"):     # both are the sales floor' in _src, "exclusion")
ck("the SMS seats for sales are unchanged (roster + the same nine)",
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
ck("/teams has the four teams, in order", [m for m in re.findall(r'id="t-([a-z]+)"', html)] ==
   ["surgical", "billing", "sales", "ringex"], re.findall(r'id="t-([a-z]+)"', html))
ck("...each from its own board's API", all(u in html for u in ("/api/v6/report?team=surgical&", "/api/v6/report?team=billing&",
                                                               "/api/v5/report?", "/api/v5/ringex?")), "urls")
style = open(os.path.join(HERE, "templates", "_board_style.html"), encoding="utf-8").read()
ck("...in the sales board's design: the same stylesheet, word for word", style in html, "style")
ck("...and the same row renderer as the board under the sales roster", "window.TB = (function" in html, "TB")

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
          "\nvar el = {innerHTML: '', querySelectorAll: function(){return [];}};\n"
          "var d = " + json.dumps({"ranked": model_rows, "totals": {"talk": 14160, "attempts": 310, "over_3": 14, "over_10": 1,
                                                                    "inbound": 3, "inbound_answered": 2, "connected": 100, "missed_open": 1},
                                   "meta": {"books": {}}}) + ";\n"
          "window.TB.render(el, window.TB.ringexModel(d, {key: 'rx', label: 'New reps'}));\n"
          "console.log(el.innerHTML);")
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
    ck("the summary says the team's totals", "<b>3:56:00</b> talk" in h and "<b>310</b> dials" in h, "sum")
    os.unlink(tmp.name)
else:
    errors.append("node is not installed -- the renderer could not be run, which is a failure, not a pass")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
