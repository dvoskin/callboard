"""Live-day pace must judge a team against ITS OWN morning, not billing's.

Danny saw agents at "200% of expected" and read it as calls being counted twice.
It is not double counting -- the dedup holds, and within a report a named agent
gets one row per interaction. It is the denominator.

Scheduling and Inbound take queue calls from the moment they log on. Billing
dials out and is barely started before 10. Measured over 160 and 174 working
agent-days from the RingCX export, inbound is 4.7% through its day at 9am where
billing's curve says 0.5%. Every seat without ten days of its own history fell
back to BILLING's curve, so an inbound agent doing an ordinary morning divided
by a denominator meant for a different team.

Two fixes, and this asserts both:

  1. each team has its own fallback curve
  2. no ratio at all below MIN_FRAC_TO_JUDGE -- at 9am the denominator is half a
     percent of a target, so one answered call reads in the hundreds of percent
     whatever curve is used. Projection already stopped there; the ratio, which
     is the number actually on the board, did not.

Run with no arguments.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod            # noqa: E402
from billing_report import (build_report, MIN_FRAC_TO_JUDGE,   # noqa: E402
                            _PACE_CURVE_DEFAULT, pace_fraction)

DAY = "2026-07-29"
AGENT = "Ariel Ramirez"


def _rows(n_calls, talk_s=600):
    return {AGENT: [{"direction": "Inbound", "result": "Accepted",
                     "duration": talk_s, "start_time": "%sT13:0%d:00+00:00" % (DAY, i % 10),
                     "source": "ringcx"} for i in range(n_calls)]}


def _pace(hour, minute, curve):
    rep = build_report(
        _rows(8), default_curve=curve, tz_offset_minutes=-240,
        window={"start": DAY, "end": DAY},
        targets=appmod.TEAM_TARGETS["inbound"],
        now_local=datetime(2026, 7, 29, hour, minute))
    for a in rep.get("ranked", []) + rep.get("stalled", []) + rep.get("silent", []):
        if a["name"] == AGENT:
            return a.get("pace")
    return None


def run():
    fails = 0
    inb = appmod.TEAM_PACE_CURVES["inbound"]

    # The curves themselves: the morning is where they diverge, and the morning
    # is where the board was wrong.
    f_bill = pace_fraction(_PACE_CURVE_DEFAULT, 10, 0)
    f_inb = pace_fraction(inb, 10, 0)
    cases = [
        ("inbound is ahead at 10am", f_inb > f_bill * 1.5, True),
        ("...and both are sane", 0 < f_bill < f_inb < 1, True),
        ("curves are monotonic",
         all(inb[i] >= inb[i - 1] for i in range(1, 24)), True),
        ("curve ends at a full day", inb[23], 1.0),
    ]

    # At 1pm there IS enough day to judge, and the team curve is the divisor.
    p_team = _pace(13, 0, inb)
    p_bill = _pace(13, 0, None)
    cases += [
        ("1pm: a ratio is given", p_team["ratio"]["talk_minutes"] is not None, True),
        ("1pm: team curve is used",
         p_team["expected"]["talk_minutes"] != p_bill["expected"]["talk_minutes"], True),
        ("1pm: judged as projectable", p_team["projectable"], True),
    ]

    # At 10:30 there is not -- and 10:30 is exactly where Danny's 222% lived.
    #
    # NOT 9am, which is what this first asked. At 9:00 the inbound curve is
    # 0.000, so expected is 0 and the ratio is None whether or not the gate
    # exists: the assertion passed with the fix removed. The hour has to be one
    # where expected is REAL but the day is still too young to divide by.
    p_early = _pace(10, 30, inb)
    cases += [
        ("10:30: expected is real", p_early["expected"]["talk_minutes"] > 0, True),
        ("10:30: but no ratio", p_early["ratio"]["talk_minutes"], None),
        ("10:30: no projection", p_early["projected"]["talk_minutes"], None),
        ("10:30: still shows actual", p_early["actual"]["talk_minutes"] > 0, True),
        ("10:30: below the bar", p_early["fraction"] < MIN_FRAC_TO_JUDGE, True),
    ]

    # The whole point: an agent doing an ORDINARY inbound morning must not read
    # as wildly ahead of a pace that is, by construction, exactly theirs.
    tgt = appmod.TEAM_TARGETS["inbound"]["talk_minutes"]["target"]

    # And against billing's curve the SAME work reads inflated -- the bug
    # reproduced, so the case above cannot be passing for an unrelated reason.
    #
    # Measured at NOON, where both curves are past the judging bar. Earlier than
    # that the threshold suppresses both and the comparison is vacuous: one
    # draft asserted 1.3x at 10:30 and passed None > None. The morning blow-up
    # (222% at 10:30, 940% at 10:00) is the THRESHOLD's to fix; what is left for
    # the curve is a steady 8-35% overstatement, 18% of it at noon.
    #
    # The work must be sized for the hour it is JUDGED at -- a second draft
    # built a 1pm workload and scored it at noon, then read the agent's own
    # 60% head start as the curve's doing.
    HOUR = 12
    on_pace_minutes = tgt * pace_fraction(inb, HOUR, 0)

    def _ratio_with(curve):
        rep = build_report(
            {AGENT: [{"direction": "Inbound", "result": "Accepted",
                      "duration": on_pace_minutes * 60, "source": "ringcx",
                      "start_time": "%sT13:00:00+00:00" % DAY}]},
            default_curve=curve, tz_offset_minutes=-240,
            window={"start": DAY, "end": DAY},
            targets=appmod.TEAM_TARGETS["inbound"],
            now_local=datetime(2026, 7, 29, HOUR, 0))
        for a in (rep.get("ranked", []) + rep.get("stalled", [])
                  + rep.get("silent", [])):
            if a["name"] == AGENT:
                return a["pace"]["ratio"]["talk_minutes"]

    rb, ri = _ratio_with(None), _ratio_with(inb)
    cases += [
        ("noon: both are judgeable", rb is not None and ri is not None, True),
        ("billing curve overstates it", (rb or 0) > (ri or 0) * 1.15, True),
        ("team curve reads on-pace", 0.95 <= (ri or 0) <= 1.05, True),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-32s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
