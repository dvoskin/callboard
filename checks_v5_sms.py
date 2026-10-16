"""Texts on the sales board (/v5): counts per rep, the cached route, the page.

Danny, 2026-10-15: "add inbound outbound sms sent auto and manual and unread
to the sales call board". Run: ./.venv/bin/python -B checks_v5_sms.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


def _r(d, frm, to, at, sent_by=None, read=None):
    r = {"direction": d, "from_number": frm, "to_number": to, "start_time": at}
    if sent_by:
        r["sent_by"] = sent_by
    if read:
        r["read"] = read
    return r


# ---- one seat's counts ----
rows = [_r("Outbound", "+12125550100", "+13055550101", "2026-10-15T14:00:00Z", sent_by="typed"),
        _r("Inbound", "+13055550101", "+12125550100", "2026-10-15T14:05:00Z", read="Read"),
        _r("Outbound", "+12125550100", "+13055550101", "2026-10-15T14:06:00Z", sent_by="auto"),
        _r("Inbound", "+13055550202", "+12125550100", "2026-10-15T15:00:00Z", read="Unread"),
        _r("Inbound", "+13055550303", "+12125550100", "2026-10-15T15:10:00Z", read="Read"),
        _r("Outbound", "+12125550100", "+13055550404", "2026-10-15T15:20:00Z", sent_by="typed")]
s = A._v5_sms_seat(rows, {"note": None})
ck("sent splits typed / auto", (s["sent"], s["sent_typed"], s["sent_auto"]) == (3, 2, 1), s)
ck("received and unread from the store's own flag", (s["received"], s["unread"]) == (3, 1), s)
ck("waiting = numbers whose newest message is inbound", s["unanswered"] == 2, s)
ck("a fully read seat is complete", s["complete"] is True, s)
s2 = A._v5_sms_seat(rows + [_r("Outbound", "+12125550100", "+13055550505", "2026-10-15T16:00:00Z")], {"note": "Hit the 12-page cap"})
ck("an outbound with no typed/auto verdict means NO split, never a guessed one", s2["sent_typed"] is None and s2["sent_auto"] is None and s2["sent"] == 4, s2)
ck("a capped read is incomplete and says why", s2["complete"] is False and "cap" in s2["note"], s2)

# ---- the build: every sales seat, totals, unresolved names reported ----
_real_seats, _real_fem = A._sms_seats, A._ringcx.fetch_extension_messages
_real_panel = A._sms_panel_seats
A._sms_panel_seats = lambda: ([], [])
A._sms_seats = lambda team, extra_names=(): ([{"name": "Roman Gomez", "ext": "146", "ext_id": 1}, {"name": "Gabriel Johnson", "ext": "162", "ext_id": 2}], ["Nobody Here"])
def _fem(ext_id, s_, e_, max_pages=6, max_wait=0.0, timeout=15.0, keep_text=False):
    assert keep_text is False, "the board never asks for text"
    return (rows, {"note": None}) if ext_id == 1 else ([], {"note": "RingEX is rate limiting the message store (HTTP 429)"})
A._ringcx.fetch_extension_messages = _fem
_sleep = A.time.sleep
A.time.sleep = lambda s: None
try:
    b = A._v5_sms_build("2026-10-15", "2026-10-15", 240)
finally:
    A._sms_seats, A._ringcx.fetch_extension_messages = _real_seats, _real_fem
    A.time.sleep = _sleep
ck("every sales seat is in the build", sorted(b["agents"]) == ["Gabriel Johnson", "Roman Gomez"] and b["agents"]["Roman Gomez"]["ext"] == "146", b["agents"])
ck("totals add up, and a seat read as NOTHING is unknown, not zero", b["totals"]["sent"] == 3 and b["totals"]["unread"] == 1
   and b["totals"]["unknown_seats"] == 1 and b["totals"]["incomplete_seats"] == 0 and b["agents"]["Gabriel Johnson"]["unknown"] is True
   and b["agents"]["Roman Gomez"]["unknown"] is False, b["totals"])

# A refused read (429) waits the cooldown out and tries again, up to three times.
_calls = []
def _fem_retry(ext_id, s_, e_, max_pages=6, max_wait=0.0, timeout=15.0, keep_text=False):
    _calls.append(ext_id)
    if ext_id == 1 and _calls.count(1) == 1:
        return [], {"note": "cooldown", "http_error": 429}
    if ext_id == 2:
        return [], {"note": "cooldown", "http_error": 429}
    return rows, {"note": None}
_slept = []
A._sms_seats = lambda team, extra_names=(): ([{"name": "Roman Gomez", "ext": "146", "ext_id": 1}, {"name": "Gabriel Johnson", "ext": "162", "ext_id": 2}], [])
A._ringcx.fetch_extension_messages = _fem_retry
A.time.sleep = lambda s_: _slept.append(s_)
try:
    b2 = A._v5_sms_build("2026-10-15", "2026-10-15", 240)
finally:
    A._sms_seats, A._ringcx.fetch_extension_messages = _real_seats, _real_fem
    A.time.sleep = _sleep
ck("a line refused once is read on the retry", b2["agents"]["Roman Gomez"]["sent"] == 3 and b2["agents"]["Roman Gomez"]["unknown"] is False and _calls.count(1) == 2, (_calls, b2["agents"]["Roman Gomez"]))
ck("a line refused three times stops trying and is unknown", _calls.count(2) == 3 and b2["agents"]["Gabriel Johnson"]["unknown"] is True, _calls)
# two paces (one per line) + three cooldown waits (one for Roman, two for
# Gabriel); no cooldown is running in a check, so each wait is the 2 s margin.
ck("reads are paced two seconds apart and each refusal waits its cooldown out", len(_slept) == 5 and A._V5_SMS_PACE == 2.0 and all(x == 2.0 for x in _slept), _slept)
ck("names the directory lacks are passed on", b["unresolved"] == ["Nobody Here"], b["unresolved"])

# ---- the other lines' table: resolved in the directory, in the order named ----
A._sms_panel_seats = _real_panel
_real_dir2 = A._ringcx.fetch_extension_directory
A._ringcx.fetch_extension_directory = lambda **k: ({
    "689": {"id": 9001, "name": "Voskin, Ariel"},           # ext matches, first name matches
    "151": {"id": 9002, "name": "Salome Kvaratskhelia"},     # ext matches
    "733": {"id": 9999, "name": "Somebody Else"},            # stale number; Anna has her own id
    "169": {"id": 701193035, "name": "Artem Bezkorovainyi"},
    "412": {"id": 9005, "name": "Makhsumov, Umed"},          # Umed has no number on file: found by name
}, {})
try:
    _ps, _pu = A._sms_panel_seats()
finally:
    A._ringcx.fetch_extension_directory = _real_dir2
_pl = {x["label"]: x for x in _ps}
ck("the five lines in the order named", [x["label"] for x in _ps] == ["Ariel", "Salome", "Anna", "Artem", "Umed"], [x["label"] for x in _ps])
ck("an extension on file is used when its first name matches the directory", _pl["Ariel"]["ext_id"] == 9001 and _pl["Salome"]["ext_id"] == 9002, _pl)
ck("a stale number does not hand Anna someone else's texts", _pl["Anna"]["ext_id"] == 683781035, _pl["Anna"])
ck("a line with no number on file is found by name ('Last, First' too)", _pl["Umed"]["ext_id"] == 9005 and _pl["Umed"]["ext"] == "412", _pl["Umed"])
A._ringcx.fetch_extension_directory = lambda **k: ({}, {})
try:
    _ps2, _pu2 = A._sms_panel_seats()
finally:
    A._ringcx.fetch_extension_directory = _real_dir2
ck("a name the directory lacks is reported, not guessed; ids on file still read",
   [x["label"] for x in _ps2] == ["Anna", "Artem"] and _pu2 == ["Ariel", "Salome", "Umed"], (_ps2, _pu2))

# the build reads them with the same paced, patient reader
A._sms_seats = lambda team, extra_names=(): ([], [])
A._sms_panel_seats = lambda: ([{"label": "Ariel", "name": "Ariel Voskin", "ext": "689", "ext_id": 1}], ["Umed"])
A._ringcx.fetch_extension_messages = _fem
A.time.sleep = lambda s_: None
try:
    b3 = A._v5_sms_build("2026-10-15", "2026-10-15", 240)
finally:
    A._sms_seats, A._ringcx.fetch_extension_messages, A._sms_panel_seats = _real_seats, _real_fem, _real_panel
    A.time.sleep = _sleep
ck("the build carries the table: in, out, unanswered, unread per line", b3["panel"] and b3["panel"][0]["label"] == "Ariel"
   and (b3["panel"][0]["received"], b3["panel"][0]["sent"], b3["panel"][0]["unanswered"], b3["panel"][0]["unread"]) == (3, 3, 2, 1)
   and b3["panel_unresolved"] == ["Umed"], b3.get("panel"))

# ---- the route: auth, loading then cached, too long refused, nothing but counts ----
c = A.app.test_client()
_real_allowed = A._v5_allowed
A._v5_allowed = lambda: False
try:
    r401 = c.get("/api/v5/sms")
finally:
    A._v5_allowed = _real_allowed
ck("signed out and no share token: refused", r401.status_code == 401, r401.status_code)

A._v5_allowed = lambda: True
_cls = type(A._ringcx); _real_cfg = _cls.__dict__.get("configured")
_cls.configured = property(lambda self: True)
_built = []
_real_build = A._v5_sms_build
A._v5_sms_build = lambda s_, e_, tz: (_built.append((s_, e_, tz)) or {"agents": {"Roman Gomez": {"sent": 3, "sent_typed": 2, "sent_auto": 1, "received": 3, "unread": 1, "unanswered": 2, "complete": True, "note": None, "ext": "146"}},
                                                                 "totals": {"sent": 3, "received": 3, "unread": 1, "unanswered": 2, "sent_typed": 2, "sent_auto": 1, "incomplete_seats": 0},
                                                                 "unresolved": [], "window": {"start": s_, "end": e_}, "seats": 1})
A._V5_SMS_CACHE.clear(); A._V5_SMS_INFLIGHT.clear()
try:
    j1 = c.get("/api/v5/sms?start=2026-10-14&end=2026-10-14&tz=240").get_json()
    for _ in range(50):
        if not A._V5_SMS_INFLIGHT:
            break
        time.sleep(0.05)
    j2 = c.get("/api/v5/sms?start=2026-10-14&end=2026-10-14&tz=240").get_json()
    j3 = c.get("/api/v5/sms?start=2026-10-14&end=2026-10-14&tz=240").get_json()
    jl = c.get("/api/v5/sms?start=2026-10-01&end=2026-10-14&tz=240").get_json()
    jb = c.get("/api/v5/sms?start=not-a-date&tz=240")
finally:
    A._v5_sms_build = _real_build
    A._v5_allowed = _real_allowed
    if _real_cfg is not None:
        _cls.configured = _real_cfg
ck("the first read of a window says loading and builds in the background", j1.get("loading") is True and _built[:1] == [("2026-10-14", "2026-10-14", 240)], (j1, _built))
ck("the next read answers from cache with its age", j2["agents"]["Roman Gomez"]["sent"] == 3 and "age_seconds" in j2 and j2.get("refreshing") is False, j2)
ck("a fresh cache is not rebuilt", len(_built) == 1 and j3["agents"]["Roman Gomez"]["unread"] == 1, _built)
ck("more than seven days is refused, not read", jl.get("too_long") is True and jl["agents"] == {}, jl)
ck("a bad date is a 400", jb.status_code == 400, jb.status_code)
ck("the route carries counts only -- no numbers, no text", "3055550" not in str(j2) and "text" not in str(j2.get("agents")), j2)

# ---- the page ----
tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v5.html"), encoding="utf-8").read()
ck("each rep's name carries its texts line", "'<span class=\"nm\" data-name=\"' + esc(a.name) + '\">' + esc(a.name) + smsLine(a.name) + '</span>'" in tpl, "name hook")
ck("the texts are fetched after every report, with the same window and token", "render(res.j);\n        smsPaint();" in tpl and "loadSms(q);" in tpl and "fetch('/api/v5/sms?' + key" in tpl, "fetch hook")
ck("the line: sent (the typed / auto split when anything was automated), received, unread in red, waiting",
   "(split && s.sent_auto ? ' <i>(' + s.sent_typed + ' typed \\u00b7 ' + s.sent_auto + ' auto)</i>' : '')" in tpl
   and "' <b>' + s.received + '</b>\\u2193'" in tpl
   and "<b class=\"hot\">' + s.unread + '</b> unread'" in tpl and "s.unanswered + ' waiting'" in tpl and "(all typed)" not in tpl, "line")
ck("the team's texts fill their row in the summary block", "var row = $('smsrow'), rv = $('smsrowv');" in tpl
   and "'</b> waiting on a reply'" in tpl and "row.hidden = false;" in tpl, "totals")
ck("names match exactly, then by first + last name", "kt.indexOf(t[0]) >= 0 && kt.indexOf(t[t.length - 1]) >= 0" in tpl, "matching")
ck("while loading it asks again, for up to four minutes", "if (++SMS_TRIES <= 30) setTimeout" in tpl, "retry")
ck("the page passes the names it shows, once per window", "'&names=' + encodeURIComponent(names.join('|'))" in tpl and "SMS_TRIES === 0" in tpl, "names")
ck("the line wraps rather than cutting off", "white-space:normal;line-height:1.35}" in tpl, "wrap")
# A name the board shows and no roster carries is read too, resolved in the directory.
_real_dir = A._ringcx.fetch_extension_directory
A._ringcx.fetch_extension_directory = lambda **k: ({"201": {"id": 777001, "name": "Rothmel Example", "type": "User", "status": "Enabled"}}, {})
try:
    _ss, _un = A._sms_seats("sales", ["Rothmel Example", "Maia Pasifae Palma"])
finally:
    A._ringcx.fetch_extension_directory = _real_dir
ck("a board-only name is resolved and read; a roster name is not doubled",
   any(x["name"] == "Rothmel Example" and x["ext"] == "201" for x in _ss) and sum(1 for x in _ss if x["name"] == "Maia Pasifae Palma") <= 1, ([x["name"] for x in _ss], _un))
ck("the table sits at the foot of the board: In, Out, Unanswered, Unread, first names",
   "'<div id=\"smspanel\" class=\"smspanel\"><div class=\"sp-h\">Texts \\u00b7 other lines</div>'" in tpl
   and "n(a.received) + n(a.sent) + n(a.unanswered, true) + n(a.unread, true)" in tpl and "esc(a.label)" in tpl and "no RingEX line found" in tpl, "panel")
ck("a line that could not be read says so instead of 0", "texts not read yet</span>" in tpl and "' line' + (T.unknown_seats === 1 ? '' : 's') + ' not read yet</i>'" in tpl, "not-read")

# ---- the message rows: read status on every inbound, the body only on request ----
rc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ringcx_client.py"), encoding="utf-8").read()
ck("every inbound row carries the store's read flag", "elif direction == \"Inbound\":\n                        row[\"read\"] = rec.get(\"readStatus\", \"\") or \"\"" in rc, "read flag")
ck("...and the body still only when asked", rc.count("row[\"text\"] = rec.get(\"subject\", \"\") or \"\"") == 1 and "if keep_text:" in rc, "text gate")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
