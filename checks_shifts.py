"""A seat with a declared shift is paced on it, not on its team's curve.

Danny, 2026-10-07: "can you pre program shift times into the boards as well",
from the schedule sheet he shared the day before. The value is in the pacing: a
team curve cannot know that Alex Morales starts at 1pm, so on the inbound curve
at 11am he read as 0% of expected -- a failing morning for a man not yet in the
building. A seat with no declared shift paces on the curve exactly as before.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
from datetime import datetime

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

errors, passed = [], 0


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


import app as A  # noqa: E402
from billing_report import build_report, shift_fraction  # noqa: E402

SH = {"start": "13:00", "end": "22:00", "off": ["sat", "sun"]}
ck("before the shift starts: 0", shift_fraction(datetime(2026, 10, 7, 11, 0), SH) == 0.0,
   shift_fraction(datetime(2026, 10, 7, 11, 0), SH))
ck("halfway through: 0.5", abs(shift_fraction(datetime(2026, 10, 7, 17, 30), SH) - 0.5) < 1e-9,
   shift_fraction(datetime(2026, 10, 7, 17, 30), SH))
ck("after it ends: 1", shift_fraction(datetime(2026, 10, 7, 23, 0), SH) == 1.0,
   shift_fraction(datetime(2026, 10, 7, 23, 0), SH))
ck("a malformed shift is None, not a crash", shift_fraction(datetime(2026, 10, 7, 12, 0), {"start": "x"}) is None,
   shift_fraction(datetime(2026, 10, 7, 12, 0), {"start": "x"}))
ck("an inverted shift is None", shift_fraction(datetime(2026, 10, 7, 12, 0), {"start": "20:00", "end": "08:00"}) is None,
   "accepted an end before its start")

# ---- the schedule sheet is on the seats ----
ck("Alex Morales starts at 1pm", A.SHIFTS["Alex Morales"]["start"] == "13:00", A.SHIFTS.get("Alex Morales"))
ck("Oscar is off Sun/Mon", A.SHIFTS["Oscar Caballero"]["off"] == ["sun", "mon"], A.SHIFTS.get("Oscar Caballero"))
surg = {x["name"]: x for x in A._billing_roster("surgical")[0]}
ck("a surgical seat carries its shift through _seat_meta",
   A._seat_meta(surg["Alex Morales"]).get("shift") == A.SHIFTS["Alex Morales"],
   A._seat_meta(surg["Alex Morales"]))
# every seat left on the surgical roster has a shift (2026-10-08), so the
# shiftless case takes one off the sheet for a moment
_sv_shift = A.SHIFTS.pop("Johana Duron")
try:
    _surg2 = {x["name"]: x for x in A._billing_roster("surgical")[0]}
    ck("a seat not on the sheet carries no shift (not a 24-hour one)",
       "shift" not in A._seat_meta(_surg2["Johana Duron"]), A._seat_meta(_surg2["Johana Duron"]))
finally:
    A.SHIFTS["Johana Duron"] = _sv_shift
bill = {x["name"]: x for x in A._billing_roster("billing")[0]}
ck("a billing seat carries its shift too",
   A._seat_meta(bill["Vivian Martinez"]).get("shift", {}).get("end") == "18:00",
   A._seat_meta(bill["Vivian Martinez"]))

# ---- live pacing: 11am, one 10am starter with a shift, one 1pm starter, one on the curve ----
NOW = datetime(2026, 10, 7, 11, 0)   # a Wednesday, 11:00 local
WIN = {"start": "2026-10-07", "end": "2026-10-07"}
def rows(n):
    return [{"direction": "Outbound", "result": "Call connected", "duration": 300,
             "start_time": "2026-10-07T14:30:00.000Z"} for _ in range(n)]
rep = build_report({
    "Judith Merlo": {"rows": rows(2), "ext": "", "ext_id": None, "complete": True,
                     "shift": {"start": "10:00", "end": "19:00", "off": []}},
    "Alex Morales": {"rows": [], "ext": "", "ext_id": None, "complete": True, "always_rank": True,
                     "shift": {"start": "13:00", "end": "22:00", "off": []}},
    "Chery Marroquin": {"rows": rows(2), "ext": "", "ext_id": None, "complete": True},
}, tz_offset_minutes=240, window=WIN, now_local=NOW,
   targets=A.TEAM_TARGETS["inbound"], default_curve=A.TEAM_PACE_CURVES["inbound"])
by = {a["name"]: a for a in rep["ranked"]}
jp, ap, cp = by["Judith Merlo"]["pace"], by["Alex Morales"]["pace"], by["Chery Marroquin"]["pace"]
ck("a declared shift is what the seat is paced on", jp["curve"] == "shift", jp["curve"])
ck("10am starter at 11am is 1/9 through her shift", abs(jp["fraction"] - 1 / 9.0) < 0.002, jp["fraction"])
ck("1pm starter at 11am has no expectation yet", ap["fraction"] == 0.0, ap["fraction"])
ck("and is NOT judged (no projection off a day not started)", ap["projectable"] is False, ap)
ck("a seat without a shift still paces on the team curve", cp["curve"] == "team", cp["curve"])
ck("the shift is on the row for the page", by["Alex Morales"]["shift"]["start"] == "13:00",
   by["Alex Morales"].get("shift"))
ck("a seat without one carries none", by["Chery Marroquin"]["shift"] is None, by["Chery Marroquin"].get("shift"))

html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html")).read()
ck("the page shows the shift on the row's panel", "bits.push('Shift ' + esc(a.shift.start)" in html, "no shift line")

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
