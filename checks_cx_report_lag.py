"""A RingCX team must be paced against the data's clock, not the wall clock.

Scheduling and Inbound are RingCX-only, and RingCX arrives by email every
fifteen minutes. At 2:14pm their board typically holds work up to about 2:00.
Pace divided that 2:00 numerator by 2:14's denominator -- one clock's work
against another clock's expectation -- and read the whole team a little behind,
every hour of every day.

It is the same defect as the 200%: numerator and denominator disagreeing. It
just points the other way, so nobody would ever have complained about it.

Two properties, and the second is the one that keeps the fix honest:

  1. a delivered report that reaches to 2:00pm paces the team as at 2:00pm
  2. a report timestamp AHEAD of now is ignored -- crediting a team for work it
     has not done yet is worse than the lag being fixed

Run with no arguments.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402


def _finish(as_of, team="inbound"):
    """Run the real tail with one seat and a known report reach."""
    roster, roster_meta = appmod._billing_roster(team)
    roster = roster[:1]
    tz = -240
    now = datetime.now(timezone.utc) + timedelta(minutes=240)
    today = now.date().isoformat()
    rows = {roster[0]["name"]: {
        "rows": [{"direction": "Inbound", "result": "Accepted", "duration": 600,
                  "start_time": "%sT13:00:00+00:00" % today, "source": "ringcx"}],
        "ext": roster[0]["ext"], "ext_id": roster[0]["ext_id"],
        "complete": True, "missing_days": []}}
    return appmod._v6_finish(rows, {"cached": 1, "fetched": 0, "missing": 0},
                             team, roster, roster_meta, today, today, tz, today,
                             [today], data_as_of=as_of)


def run():
    fails = 0
    now = datetime.now(timezone.utc) + timedelta(minutes=240)

    # A report reaching to an hour ago must pull the pace clock back to it.
    behind = (now - timedelta(hours=1)).strftime("%H:%M:%S")
    r_lag = _finish(behind)
    r_now = _finish(None)

    def frac(rep):
        for a in (rep.get("ranked", []) + rep.get("stalled", [])
                  + rep.get("silent", [])):
            if a.get("pace"):
                return a["pace"]["fraction"]

    f_lag, f_now = frac(r_lag), frac(r_now)
    cases = [
        ("lagged report is reported", bool(r_lag.get("data_as_of")), True),
        ("...with the minutes behind",
         55 <= (r_lag.get("data_as_of") or {}).get("lag_minutes", 0) <= 65, True),
        ("live source says nothing", r_now.get("data_as_of"), None),
        ("both produce a pace", f_lag is not None and f_now is not None, True),
        ("lagged pace is not ahead of live", (f_lag or 0) <= (f_now or 0), True),
    ]

    # A timestamp in the FUTURE must be ignored, not used to credit work that
    # has not happened. This is the direction that would flatter the team.
    #
    # Two distinct cases, and running only the first left the lower bound
    # untested: +2h WRAPS past midnight late in the evening, so it arrives back
    # as a positive 22-hour "lag" and the UPPER bound catches it. Removing
    # `0 <` changed nothing and the mutation stayed green. A same-day future
    # time is the only thing that exercises it.
    wrapped = (now + timedelta(hours=2)).strftime("%H:%M:%S")
    r_wrapped = _finish(wrapped)
    cases += [
        ("wrapped timestamp ignored", r_wrapped.get("data_as_of"), None),
        ("...and pace matches live", frac(r_wrapped), f_now),
    ]

    # The bounds themselves, on a FIXED clock. Driven through _v6_finish these
    # borrow the real wall clock, and the same-day-future case cannot even be
    # constructed after 10:30pm -- which is when this was first written, so it
    # printed "not applicable" and the lower bound went unverified.
    noon = datetime(2026, 8, 25, 12, 0)
    rc = appmod._reconcile_report_clock
    for label, arg, want_clock, want_note in [
        ("60 min behind accepted", "11:00:00", datetime(2026, 8, 25, 11, 0), True),
        ("15 min behind accepted", "11:45:00", datetime(2026, 8, 25, 11, 45), True),
        ("same-day FUTURE ignored", "14:00:00", noon, False),
        ("one minute ahead ignored", "12:01:00", noon, False),
        ("exactly now ignored", "12:00:00", noon, False),
        ("beyond the lag cap ignored", "02:00:00", noon, False),
        ("junk ignored", "zz:zz:zz", noon, False),
        ("empty ignored", "", noon, False),
    ]:
        clock, note = rc(noon, arg)
        cases.append((label, (clock, note is not None), (want_clock, want_note)))

    # Garbage must not take the board down or silently shift the clock.
    for bad in ("", "not-a-time", "99:99:99", None):
        r = _finish(bad)
        cases.append(("junk %r is ignored" % (bad,), r.get("data_as_of"), None))

    # END TO END, producer into consumer. Both halves were tested and the SEAM
    # was not: _v6_cx_rows_for_team hands over the row's own timestamp, which
    # RingCX writes date-first as "07/29/2026 22:17:49", and the consumer read
    # the first two characters as an hour. The 29th became 7:29am with a
    # 271-minute "lag" to match, and it would have applied silently every
    # mid-morning. Feed a REAL watermark, never a hand-written one.
    roster, _rm = appmod._billing_roster("inbound")
    day = "2026-07-29"
    _by, _days, covers = appmod._v6_cx_rows_for_team("inbound", [day], roster)
    wm = covers.get(day)
    if wm:
        hh = int(str(wm).split()[-1].split(":")[0])
        mm = int(str(wm).split()[-1].split(":")[1])
        after = datetime(2026, 7, 29, 23, 59)
        clock, note = appmod._reconcile_report_clock(after, wm)
        cases += [
            ("real watermark has a date", " " in str(wm), True),
            ("...hour read from the TIME", clock.hour, hh),
            ("...minute too", clock.minute, mm),
            ("...and a believable lag",
             0 < (note or {}).get("lag_minutes", 0) <= 360, True),
        ]
    else:
        raise SystemExit("no watermark produced -- the check cannot run, which "
                         "is a failure, not a pass")

    for label, got, want in cases:
        ok = got == want
        print("  %-34s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
