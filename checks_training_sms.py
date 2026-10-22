"""Training class merged into sales (2026-10-22).

The separate loop:ringex, _tc_sms_pass, and cache_only_today infrastructure
is gone. The five active RingEX reps are now regular sales seats, and their
texts are read on demand like any other RingEX seat.

Run: ./.venv/bin/python -B checks_training_sms.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


FIVE = ["Annyta Santiago", "Ramon Gallardo", "Roman Gomez", "Jabes Perdomo", "Seydi Medrano"]

ck("no ringex team roster exists", "ringex" not in A._TEAM_ROSTERS, list(A._TEAM_ROSTERS.keys()))
ck("the five RingEX reps are on the sales roster",
   all(any(s["name"] == n for s in A._TEAM_ROSTERS["sales"]) for n in FIVE), FIVE)
ck("each has source=ringex and an ext_id",
   all(s.get("source") == "ringex" and s.get("ext_id") for s in A._TEAM_ROSTERS["sales"] if s["name"] in FIVE),
   [(s["name"], s.get("source")) for s in A._TEAM_ROSTERS["sales"] if s["name"] in FIVE])
ck("none of the five has cache_only_today",
   all(not s.get("cache_only_today") for s in A._TEAM_ROSTERS["sales"] if s["name"] in FIVE),
   [s.get("cache_only_today") for s in A._TEAM_ROSTERS["sales"] if s["name"] in FIVE])
ck("SMS is enabled for the sales team (no longer disabled)",
   A._team_sms_enabled("sales"), A._TEAM_SMS)
ck("loop:ringex is not in worker loops",
   not any(n == "loop:ringex" for n, _ in A._WORKER_LOOPS), [n for n, _ in A._WORKER_LOOPS])

# the SMS extra seats keep departed reps' lines for the STOP scan
ck("departed reps' lines are in _SMS_EXTRA_SEATS for the STOP scan",
   len(A._SMS_EXTRA_SEATS.get("sales", [])) >= 1
   and all(s.get("ext_id") for s in A._SMS_EXTRA_SEATS.get("sales", [])), A._SMS_EXTRA_SEATS.get("sales"))

# the teamboard renderer still exists (shared code)
tb = open(os.path.join(HERE, "templates", "_teamboard.html"), encoding="utf-8").read()
ck("the shared teamboard renderer still has ringexModel (it renders RingEX-sourced seats)",
   "function ringexModel" in tb, "ringexModel")

# /v5 no longer has the training class board
v5 = open(os.path.join(HERE, "templates", "scoreboard_v5.html"), encoding="utf-8").read()
ck("/v5 has no #exout or loadEx", 'id="exout"' not in v5 and "loadEx" not in v5, "exout")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print()
print("%d failed" % len(errors))
sys.exit(1 if errors else 0)
