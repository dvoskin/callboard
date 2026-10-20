"""Texts for the Sales Training Class (Danny, 2026-10-20: "add the inbound
outbound sms and unread / responded for the training team as well").

What must hold:
  - in / out / responded / unanswered per rep, counted the way the coordinators'
    and billers' boards count them (billing_report `sms` + `_sms_replies`);
  - today's texts are read ONLY by loop:ringex (_tc_sms_pass), never by a page
    load: the request path serves the loop's last reading, marked by its age;
  - a rep not read yet is "not read yet" (unknown), never zero, and a total
    says how many reps it is missing;
  - the loop stands down in a message-store cooldown and stops at a refusal;
  - /v5's own texts build no longer reads these six (one read, not two).

Run: ./.venv/bin/python -B checks_training_sms.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A  # noqa: E402
import pathlib  # noqa: E402

# Every reading this check writes goes to a temp folder, never the worktree
# (a leftover v6_days/_today makes checks_teams read "billing seat still fetches").
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="tc_sms_"))
A.V6_SNAP_DIR, A.V6_TODAY_DIR = _TMP / "v6_days", _TMP / "v6_days" / "_today"
A.V6_SMS_DIR, A.V6_SMS_TODAY_DIR = _TMP / "v6_sms", _TMP / "v6_sms" / "_today"
A._v6_today_cache.clear()
A._v6_sms_today_cache.clear()

HERE = os.path.dirname(os.path.abspath(__file__))
passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


TZ = 240
now = datetime.now(timezone.utc)
today = (now - timedelta(minutes=TZ)).date().isoformat()
seats = A._billing_roster("ringex")[0]
by_name = {s["name"]: s for s in seats}
ROMAN, AMBAR = by_name["Roman Gomez"], by_name["Ambar Luciano"]
OURS = "+12125550146"


def t(mins_ago):
    return (now - timedelta(minutes=mins_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def msg(d, other, mins_ago, sent_by=None):
    r = {"direction": d, "start_time": t(mins_ago),
         "from_number": other if d == "Inbound" else OURS, "to_number": OURS if d == "Inbound" else other}
    if sent_by:
        r["sent_by"] = sent_by
    return r


# Patient A wrote, the rep answered; patient B wrote and waits; the rep texted C.
ROMAN_ROWS = [msg("Inbound", "+13055550101", 50), msg("Outbound", "+13055550101", 45, "typed"),
              msg("Inbound", "+13055550202", 30), msg("Outbound", "+13055550303", 20, "auto")]

# ---- every RingEX read is counted; a call-log read is a failure ----
reads = []
_real_fem, _real_rl = A._ringcx.fetch_extension_messages, A._ringcx.rate_limited
REFUSE = set()


def _fem(ext_id, s_, e_, max_pages=6, max_wait=0.0, timeout=15.0, keep_text=False):
    reads.append(ext_id)
    if ext_id in REFUSE:
        return [], {"note": "RingEX is rate limiting the message store (HTTP 429)", "http_error": 429}
    return (list(ROMAN_ROWS) if ext_id == ROMAN["ext_id"] else []), {"rate_group": "sms"}


def _no_calls(*a, **k):
    raise AssertionError("a call-log read on a texts path")


A._ringcx.fetch_extension_messages = _fem
COOL = {"sms": False}
A._ringcx.rate_limited = lambda group=None: COOL["sms"] if group == "sms" else False
_real_day, _real_diag = A._v6_fetch_day, A._v6_fetch_day_diag
A._v6_fetch_day = _no_calls
A._v6_fetch_day_diag = _no_calls
A._TC_SMS_PAUSE = 0.0

try:
    ck("texts are on for the training class, still off for the RingCX floor, on for billing",
       A._team_sms_enabled("ringex") and not A._team_sms_enabled("sales") and A._team_sms_enabled("billing"), A._TEAM_SMS)
    ck("every class seat is cache_only_today (carried through _billing_roster)",
       len(seats) == 6 and all(s.get("cache_only_today") for s in seats), [s.get("cache_only_today") for s in seats])

    # ---- the request path never reads today's texts for the class ----
    out, st = A._v6_fetch_sms(seats, [today], today, TZ)
    ck("a page load reads NO message store for the class", reads == [], reads)
    ck("...a rep the loop has not reached is not read yet, with the reason -- never an empty, complete day",
       out["Roman Gomez"]["complete"] is False and "not read yet" in (out["Roman Gomez"]["note"] or ""), out["Roman Gomez"])
    # a billing seat's today is still read on request, as before
    bill = A._billing_roster("billing")[0][:1]
    if bill:
        A._v6_fetch_sms(bill, [today], today, TZ)
        ck("...while a billing seat's today is still read on request (unchanged)", reads == [bill[0]["ext_id"]], reads)
    reads.clear()

    # ---- the loop: one read per rep, then nothing until the reading ages ----
    n = A._tc_sms_pass(today, TZ)
    ck("the loop reads each of the six once", n == 6 and sorted(reads) == sorted(s["ext_id"] for s in seats), (n, reads))
    reads.clear()
    ck("...and none again while the readings are fresh", A._tc_sms_pass(today, TZ) == 0 and reads == [], reads)
    ck("...the reading is on disk too, so a restart does not blank it",
       (A.V6_SMS_TODAY_DIR / str(ROMAN["ext_id"]) / ("%s.json" % today)).exists(), str(A.V6_SMS_TODAY_DIR))
    # age every reading; a cooldown in the message store's group stands the loop down
    for s in seats:
        A._v6_sms_today_cache[(s["ext_id"], today)]["at"] -= A._TC_REFRESH_AFTER + 5
    COOL["sms"] = True
    y0 = A._ringex_warm_state["sms_yielded"]
    ck("in a message-store cooldown the loop reads nothing and says it yielded",
       A._tc_sms_pass(today, TZ) == 0 and reads == [] and A._ringex_warm_state["sms_yielded"] == y0 + 1, reads)
    COOL["sms"] = False
    REFUSE.add(seats[0]["ext_id"])
    A._tc_sms_pass(today, TZ)
    ck("...and one refusal stops the pass (the minute's budget is gone)", reads == [seats[0]["ext_id"]], reads)
    REFUSE.clear()
    reads.clear()
    A._tc_sms_pass(today, TZ)
    reads.clear()

    # Ambar: no reading at all (as after a restart with nothing on disk)
    A._v6_sms_today_cache.pop((AMBAR["ext_id"], today), None)
    p = A.V6_SMS_TODAY_DIR / str(AMBAR["ext_id"]) / ("%s.json" % today)
    if p.exists():
        p.unlink()
    # Annyta: a reading 30 minutes old
    ANNYTA = by_name["Annyta Santiago"]
    A._v6_sms_today_cache[(ANNYTA["ext_id"], today)]["at"] = A.time.time() - 1800

    # ---- the real request path: /api/v5/ringex ----
    for s in seats:
        A._v6_today_put(s["ext_id"], today, [])          # calls: read, quiet
    _real_allowed, _real_books = A._v5_allowed, A._v5_books
    _cfg = type(A._ringcx).configured
    A._v5_allowed = lambda: True
    A._v5_books = lambda s_, e_: ({}, {"loading": False})
    type(A._ringcx).configured = property(lambda self: True)
    try:
        with A.app.test_request_context("/api/v5/ringex?tz=%d&start=%s&end=%s" % (TZ, today, today)):
            resp = A.api_v5_ringex()
        j = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    finally:
        A._v5_allowed, A._v5_books = _real_allowed, _real_books
        type(A._ringcx).configured = _cfg
    ck("serving the class reads NO message store and NO call log", reads == [], reads)
    rows = {r["name"]: r for r in j.get("ranked") or []}
    rg = (rows.get("Roman Gomez") or {}).get("sms") or {}
    ck("Roman: 2 in, 2 out (1 typed, 1 auto)", (rg.get("received"), rg.get("sent"), rg.get("sent_typed"), rg.get("sent_auto")) == (2, 2, 1, 1), rg)
    ck("...1 responded (patient A answered), 1 unanswered (patient B wrote last)",
       (rg.get("replied"), rg.get("unanswered")) == (1, 1), rg)
    # the same figures billing_report gives a coordinator for the same rows
    import billing_report
    br = billing_report._sms_replies(ROMAN_ROWS, TZ)
    ck("...exactly what the coordinators' board computes for the same texts",
       (br["replied"], br["unanswered"]) == (rg.get("replied"), rg.get("unanswered")), br)
    am = (rows.get("Ambar Luciano") or {}).get("sms") or {}
    ck("Ambar, never read: unknown -- not read yet, never zero", am.get("unknown") is True and am.get("complete") is False, am)
    an = (rows.get("Annyta Santiago") or {}).get("sms") or {}
    ck("Annyta, read 30 min ago: shown, complete, and marked by its age", an.get("unknown") is False
       and an.get("complete") is True and an.get("stale_minutes") == 30, an)
    T = (j.get("totals") or {}).get("sms") or {}
    ck("the class total sums only reps that were read and says how many were not",
       (T.get("received"), T.get("sent"), T.get("replied"), T.get("unanswered"), T.get("seats"), T.get("seats_total"), T.get("unknown_seats"))
       == (2, 2, 1, 1, 5, 6, 1), T)

    # ---- /v5's own texts build skips the class (one read, not two) ----
    _real_seats, _real_panel, _real_pace = A._sms_seats, A._sms_panel_seats, A._V5_SMS_PACE
    A._sms_seats = lambda team, extra_names=(): ([dict(ROMAN), {"name": "Adelita Flowers", "ext": "120", "ext_id": 777}], [])
    A._sms_panel_seats = lambda: ([], [])
    A._V5_SMS_PACE = 0.0
    try:
        d = A._v5_sms_build(today, today, TZ)
    finally:
        A._sms_seats, A._sms_panel_seats, A._V5_SMS_PACE = _real_seats, _real_panel, _real_pace
    ck("/v5's texts build reads the floor only", reads == [777] and list(d["agents"]) == ["Adelita Flowers"], (reads, list(d["agents"])))
    ck("...and names the class as read elsewhere, so a board asking for them is not 'not covered' forever",
       d.get("elsewhere") == ["Roman Gomez"], d.get("elsewhere"))
finally:
    A._ringcx.fetch_extension_messages, A._ringcx.rate_limited = _real_fem, _real_rl
    A._v6_fetch_day, A._v6_fetch_day_diag = _real_day, _real_diag

# ---- the pages ----
tb = open(os.path.join(HERE, "templates", "_teamboard.html"), encoding="utf-8").read()
v5 = open(os.path.join(HERE, "templates", "scoreboard_v5.html"), encoding="utf-8").read()
ck("the class's rows carry texts unanswered under the name, beside missed calls",
   "var bits = [pressMissed(a.missed_open, true), smsPress(a.sms)].filter(Boolean);" in tb, "press")
ck("...in / out and responded / unanswered in the panel, 'not read yet' for an unread rep",
   ".concat(smsCells(a.sms))" in tb and "[['Texts', '<i>not read yet</i>', true]]" in tb, "panel")
ck("...a Texts line in the summary, with the reps not read yet named as a count",
   ".concat(allUnread ? [] : smsSum(T.sms))" in tb and "' not read yet'" in tb, "sum")
ck("/v5 no longer paints the floor's texts over the class's rows",
   "if (el.closest('#exout')) return;" in v5 and "smsLine(a.name)" not in v5.split("function loadEx")[1].split("function cell(")[0], "v5")
ck("one summary label column on every board (62px; 54px on a phone)",
   ".sum .srow{display:grid;grid-template-columns:62px minmax(0,1fr)" in open(os.path.join(HERE, "templates", "_board_style.html"), encoding="utf-8").read()
   and "grid-template-columns:62px minmax(0,1fr);gap:0 10px" not in tb, "label column")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print()
print("%d failed" % len(errors))
sys.exit(1 if errors else 0)
