"""billing_report must reproduce the hand-verified 2026-05-26..08-23 figures.

Those ninety days were pulled per extension from the RingEX call log and every
number below was checked against the raw records before the KPI baseline was
published. This feeds the same row shapes through build_report so the LOGIC is
verified without credentials.

The case that matters most is the talk-time definition. RingEX reports a nonzero
`duration` on calls that were never answered; summing it raw added 31 hours
across these four seats. So there is an explicit negative case here: a set of
unanswered rows with large durations must contribute ZERO talk time. A positive
test alone would pass just as happily with the bug in place.

Run with no arguments. If the fixture pull is present it also re-checks against
the real 90-day extract; otherwise the synthetic cases still run.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from billing_report import (build_report, is_connected, DEFAULT_TARGETS, grade,
                            build_pace_curve, pace_fraction, MIN_DAYS_FOR_OWN_CURVE,
                            _PACE_CURVE_DEFAULT)

TZ = -240  # US/Eastern in May-August

# The four seats and their verified 90-day totals (working days = days with at
# least one CONNECTED call; Ana measured through Aug 8, when her line went dead).
VERIFIED = {
    "Vivian Martinez":    {"days": 80, "calls": 6965, "connected": 5903, "long": 901},
    "Yareth Pavon":       {"days": 60, "calls": 3407, "connected": 2800, "long": 533},
    "Gabriela Maldonado": {"days": 63, "calls": 2745, "connected": 1134, "long": 413},
    # Ana over the FULL window; her line went dead on Aug 10, which is why the
    # published baseline cut her at Aug 8. The extra working day is Aug 11, where
    # one call connected.
    "Ana Salazar":        {"days": 55, "calls": 4532, "connected": 3720, "long": 663},
}
EXT_FILES = {
    "Vivian Martinez": 405657034, "Yareth Pavon": 998743035,
    "Gabriela Maldonado": 1027587035, "Ana Salazar": 436846034,
}
FIXTURE_DIR = os.environ.get("BILLING_FIXTURES", "")


def _local_date(ts):
    """UTC ISO -> local (Eastern) date string, matching build_report's bucketing."""
    from datetime import datetime, timedelta, timezone
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (d.astimezone(timezone.utc) + timedelta(minutes=TZ)).date().isoformat()
    except (ValueError, TypeError):
        return None


def _row(direction, result, duration, start="2026-06-01T14:00:00.000Z"):
    return {"direction": direction, "result": result, "duration": duration,
            "start_time": start}


def case_ring_time_is_not_talk():
    """THE regression case. Unanswered calls carry a duration; it is ring time.

    Every row here is a real (direction, result) pair observed on this account,
    with the real maximum duration seen for it. If any of them is ever counted,
    talk time inflates and the KPI moves under everyone's feet.
    """
    rows = [
        _row("Outbound", "Hang Up", 80),        # ring timeout, not an 80s call
        _row("Outbound", "No Answer", 80),      # every single one of these is 80s
        _row("Inbound", "Missed", 1037),        # rang 17 minutes in a queue
        _row("Inbound", "Voicemail", 974),      # a long voicemail is not a talk
        _row("Outbound", "Wrong Number", 75),
        _row("Outbound", "Call Failed", 75),
        _row("Outbound", "Busy", 32),
        _row("Inbound", "Reply", 98),
        _row("Outbound", "Not Allowed", 0),
        _row("Inbound", "Blocked", 0),
    ]
    rep = build_report({"Tester": {"rows": rows, "ext": "999"}}, tz_offset_minutes=TZ,
                       window={"start": "2026-06-01", "end": "2026-06-01"})
    fails = 0
    # Dialled all window, connected nothing -> "stalled", NOT ranked with a zero.
    # Ranking it last would say the seat competed; it did not, it is broken.
    seat = (rep["stalled"] + rep["silent"] + rep["ranked"])[0]
    t = seat["totals"]
    for label, got, want in [
        ("talk_seconds", t["talk_seconds"], 0),
        ("connected", t["connected"], 0),
        ("over_120s", t["over_120s"], 0),
        ("calls", t["calls"], len(rows)),
        ("handled_calls", t["handled_calls"], 6),   # 6 outbound; no inbound picked up
        ("worked_days", seat["worked_days"], 0),
        ("classed stalled", len(rep["stalled"]), 1),
        ("kept out of rank", len(rep["ranked"]), 0),
    ]:
        ok = got == want
        print("  %-16s want %-6s got %-6s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_empty_seat_is_silent():
    """No rows at all is a DIFFERENT state from dialled-but-never-connected. One
    is a wrong extension, the other is a broken line, and the fix differs."""
    rep = build_report({"Nobody": {"rows": [], "ext": "999"}}, tz_offset_minutes=TZ,
                       window={"start": "2026-06-01", "end": "2026-06-01"})
    fails = 0
    for label, got, want in [
        ("silent", len(rep["silent"]), 1),
        ("stalled", len(rep["stalled"]), 0),
        ("ranked", len(rep["ranked"]), 0),
        ("warns", any(w["kind"] == "no_activity" for w in rep["warnings"]), True),
    ]:
        ok = got == want
        print("  %-16s want %-6s got %-6s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_failed_fetch_is_not_zero():
    """THE bug this was written after. A rate-limited fetch returns [] exactly
    like a phone that never rang. Rendering that as "logged no calls at all" on a
    board that names individuals turns an HTTP 429 into an accusation -- which is
    what the first live 28-day run actually did to three of four seats."""
    rep = build_report({
        "Read OK":     {"rows": [], "ext": "1", "complete": True},
        "Fetch failed": {"rows": [], "ext": "2", "complete": False,
                         "missing_days": ["2026-06-01", "2026-06-02"]},
    }, tz_offset_minutes=TZ, window={"start": "2026-06-01", "end": "2026-06-02"})
    fails = 0
    silent = [a["name"] for a in rep["silent"]]
    unknown = [a["name"] for a in rep["unknown"]]
    for label, got, want in [
        ("silent names", silent, ["Read OK"]),
        ("unknown names", unknown, ["Fetch failed"]),
        ("no false accusation", any("Fetch failed" in w["message"] and "no calls at all"
                                    in w["message"] for w in rep["warnings"]), False),
        ("says not known", any(w["kind"] == "unknown_seat" for w in rep["warnings"]), True),
    ]:
        ok = got == want
        print("  %-20s want %-16s got %-16s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_connected_is_counted():
    """The positive half: connected calls DO contribute, and the 2-minute mark
    counts connected calls only."""
    rows = [
        _row("Outbound", "Call connected", 300),   # 5 min  -> over 120
        _row("Outbound", "Call connected", 119),   # just under
        _row("Inbound", "Accepted", 121),          # just over
        _row("Inbound", "Missed", 3000),           # must not count anywhere
    ]
    rep = build_report({"Tester": {"rows": rows, "ext": "999"}}, tz_offset_minutes=TZ,
                       window={"start": "2026-06-01", "end": "2026-06-01"})
    a = rep["ranked"][0]
    t = a["totals"]
    fails = 0
    for label, got, want in [
        ("talk_seconds", t["talk_seconds"], 540),      # 300+119+121, NOT +3000
        ("connected", t["connected"], 3),
        ("over_120s", t["over_120s"], 2),              # 300 and 121
        ("over_60s", t["over_60s"], 3),
        ("longest", t["longest_seconds"], 300),
        ("inbound", t["inbound"], 2),
        ("inbound_answered", t["inbound_answered"], 1),
        ("answer_rate", a["answer_rate_pct"], 50.0),
        ("worked_days", a["worked_days"], 1),
        ("avg_call", a["avg_call_seconds"], 180),
        # 2 outbound + 1 inbound answered. The Missed inbound is NOT work handled.
        ("handled_calls", t["handled_calls"], 3),
        ("calls/day", a["per_day"]["calls"], 3.0),
    ]:
        ok = got == want
        print("  %-16s want %-6s got %-6s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_idle_days_excluded():
    """A day that dialled but connected nothing is a day off, not a zero. It must
    not drag the per-day average down, and it must be reported as idle."""
    rows = [
        _row("Outbound", "Call connected", 600, "2026-06-01T14:00:00.000Z"),
        _row("Outbound", "Hang Up", 20, "2026-06-06T14:00:00.000Z"),   # Saturday, nothing connected
        _row("Outbound", "Hang Up", 20, "2026-06-07T14:00:00.000Z"),
    ]
    rep = build_report({"Tester": {"rows": rows, "ext": "999"}}, tz_offset_minutes=TZ,
                       window={"start": "2026-06-01", "end": "2026-06-07"})
    a = rep["ranked"][0]
    fails = 0
    for label, got, want in [
        ("worked_days", a["worked_days"], 1),
        ("idle_days", a["idle_days"], 2),
        ("talk_min/day", a["per_day"]["talk_minutes"], 10.0),   # 600s over ONE day, not three
    ]:
        ok = got == want
        print("  %-16s want %-6s got %-6s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_unknown_result_warns():
    """An unrecognised result must raise a warning, not silently become
    'not connected'. An absent answer must never read as a negative answer."""
    rows = [_row("Outbound", "Teleported", 240),
            _row("Outbound", "Call connected", 240)]
    rep = build_report({"Tester": {"rows": rows, "ext": "999"}}, tz_offset_minutes=TZ,
                       window={"start": "2026-06-01", "end": "2026-06-01"})
    kinds = [w["kind"] for w in rep["warnings"]]
    ok = "unknown_result" in kinds
    print("  %-16s want %-6s got %-6s %s" % ("warns", True, ok, "OK" if ok else "<<< FAIL"))
    vals = [w for w in rep["warnings"] if w["kind"] == "unknown_result"]
    named = bool(vals) and any(v[0] == "Teleported" for v in vals[0]["values"])
    print("  %-16s want %-6s got %-6s %s" % ("names it", True, named, "OK" if named else "<<< FAIL"))
    return (0 if ok else 1) + (0 if named else 1)


def case_grading():
    """Band edges are inclusive at the bottom of each tier."""
    spec = DEFAULT_TARGETS["talk_minutes"]   # 60 / 85 / 110
    fails = 0
    for value, want in [(59.9, "below"), (60, "floor"), (84.9, "floor"),
                        (85, "target"), (109.9, "target"), (110, "stretch"), (999, "stretch")]:
        got = grade(value, spec)
        ok = got == want
        print("  %-16s want %-8s got %-8s %s" % (value, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_fixture():
    """Re-check against the real 90-day extract, when it is on disk."""
    if not FIXTURE_DIR or not os.path.isdir(FIXTURE_DIR):
        print("  (skipped: set BILLING_FIXTURES to the dir holding calls_<extid>.json)")
        return 0
    rows_by_agent, missing = {}, []
    for name, ext in EXT_FILES.items():
        p = os.path.join(FIXTURE_DIR, "calls_%d.json" % ext)
        if not os.path.exists(p):
            missing.append(name)
            continue
        raw = json.load(open(p))
        # The fixture holds the raw pull, which runs a day past the verified
        # window. Production never sees that -- the API call is bounded by
        # dateFrom/dateTo -- so clip here rather than letting extra days walk
        # into the comparison and look like a logic error.
        rows = []
        for r in raw:
            t = _local_date(r.get("startTime"))
            if t and "2026-05-26" <= t <= "2026-08-23":
                rows.append({"direction": r.get("direction"), "result": r.get("result"),
                             "duration": r.get("duration"), "start_time": r.get("startTime")})
        rows_by_agent[name] = {"rows": rows, "ext": ""}
    if missing:
        print("  (missing fixtures for: %s)" % ", ".join(missing))
    if not rows_by_agent:
        print("  (skipped: no fixture files found)")
        return 0
    rep = build_report(rows_by_agent, tz_offset_minutes=TZ,
                       window={"start": "2026-05-26", "end": "2026-08-23"})
    by = {a["name"]: a for a in rep["ranked"] + rep["silent"]}
    fails = 0
    for name, want in VERIFIED.items():
        a = by.get(name)
        if not a:
            print("  %-20s <<< FAIL (absent from report)" % name)
            fails += 1
            continue
        got = {"days": a["worked_days"], "calls": a["worked"]["calls"],
               "connected": a["worked"]["connected"], "long": a["worked"]["over_120s"]}
        ok = all(got[k] == want[k] for k in want)
        print("  %-20s want %s got %s %s" % (name, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_pace_curve():
    """The curve must beat a clock pro-rate, stay monotonic, and refuse to build
    itself from too little history."""
    fails = 0
    # 1. Not the clock. At 2pm a 9-to-6 model says 56%; the measured figure is 37%.
    at2pm = pace_fraction(None, 14, 0)
    ok = 0.30 < at2pm < 0.42
    print("  %-24s want %-14s got %-14s %s"
          % ("2pm != linear 56%", "0.30-0.42", round(at2pm, 3), "OK" if ok else "<<< FAIL"))
    fails += 0 if ok else 1
    # 2. Monotonic, and interpolating inside the hour.
    prev, mono = -1, True
    for hr in range(24):
        for mn in (0, 30):
            f = pace_fraction(None, hr, mn)
            if f < prev - 1e-9:
                mono = False
            prev = f
    print("  %-24s want %-14s got %-14s %s"
          % ("never goes backwards", True, mono, "OK" if mono else "<<< FAIL"))
    fails += 0 if mono else 1
    mid = pace_fraction(None, 13, 30)
    between = pace_fraction(None, 13, 0) < mid < pace_fraction(None, 14, 0)
    print("  %-24s want %-14s got %-14s %s"
          % ("interpolates in-hour", True, between, "OK" if between else "<<< FAIL"))
    fails += 0 if between else 1
    # 3. Too little history -> None, so the caller falls back to the team curve.
    thin = [{10: 5} for _ in range(MIN_DAYS_FOR_OWN_CURVE - 1)]
    got = build_pace_curve(thin)
    print("  %-24s want %-14s got %-14s %s"
          % ("thin history -> None", None, got, "OK" if got is None else "<<< FAIL"))
    fails += 0 if got is None else 1
    # 4. A late-shift seat gets a LATE curve, not the team's. This is the
    #    Gabriela case: judged on the team curve she reads behind all morning.
    late = [{14: 10, 15: 10, 16: 10, 17: 10} for _ in range(12)]
    c = build_pace_curve(late)
    own_noon, team_noon = pace_fraction(c, 12, 0), pace_fraction(None, 12, 0)
    ok = c is not None and own_noon < team_noon
    print("  %-24s want %-14s got %-14s %s"
          % ("late shift < team @ noon", "own<team",
             "%.2f<%.2f" % (own_noon, team_noon), "OK" if ok else "<<< FAIL"))
    fails += 0 if ok else 1
    return fails


def case_pace_projection():
    """Live-day pace: expected scales with the curve, and an early sliver of the
    day must NOT be extrapolated into a heroic projection."""
    from datetime import datetime
    rows = [_row("Outbound", "Call connected", 1200, "2026-06-01T14:00:00.000Z")]  # 10am ET, 20 min
    fails = 0

    # 9:05am -- ~0.5% of the day gone. 20 minutes of talk must not project to hours.
    early = build_report({"T": {"rows": rows, "ext": "1"}}, tz_offset_minutes=TZ,
                         window={"start": "2026-06-01", "end": "2026-06-01"},
                         now_local=datetime(2026, 6, 1, 9, 5))
    p = early["ranked"][0]["pace"]
    for label, got, want in [
        ("live", early["live"], True),
        ("no projection", p["projected"]["talk_minutes"], None),
        ("projectable flag", p["projectable"], False),
        ("grade withheld", p["grades"]["talk_minutes"], None),
    ]:
        ok = got == want
        print("  %-24s want %-10s got %-10s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # 2pm -- ~37% gone, so 20 min of talk projects to ~54 and expected is ~31.
    mid = build_report({"T": {"rows": rows, "ext": "1"}}, tz_offset_minutes=TZ,
                       window={"start": "2026-06-01", "end": "2026-06-01"},
                       now_local=datetime(2026, 6, 1, 14, 0))
    p = mid["ranked"][0]["pace"]
    exp, proj = p["expected"]["talk_minutes"], p["projected"]["talk_minutes"]
    for label, got, ok in [
        ("expected pro-rated", exp, 25 < exp < 38),
        ("projected from pace", proj, 45 < proj < 70),
        ("ratio < 1 (behind)", p["ratio"]["talk_minutes"], p["ratio"]["talk_minutes"] < 1),
    ]:
        print("  %-24s got %-10s %s" % (label, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    # A multi-day window is never "live".
    rng = build_report({"T": {"rows": rows, "ext": "1"}}, tz_offset_minutes=TZ,
                       window={"start": "2026-06-01", "end": "2026-06-07"},
                       now_local=datetime(2026, 6, 1, 14, 0))
    ok = rng["live"] is False and rng["ranked"][0]["pace"] is None
    print("  %-24s want %-10s got %-10s %s" % ("range is not live", True, ok, "OK" if ok else "<<< FAIL"))
    fails += 0 if ok else 1
    return fails


def run():
    total = 0
    for title, fn in [
        ("ring time is NOT talk time", case_ring_time_is_not_talk),
        ("empty seat is silent, not stalled", case_empty_seat_is_silent),
        ("failed fetch is NOT a zero score", case_failed_fetch_is_not_zero),
        ("connected calls ARE counted", case_connected_is_counted),
        ("idle days excluded from averages", case_idle_days_excluded),
        ("unknown result warns", case_unknown_result_warns),
        ("KPI band edges", case_grading),
        ("intraday pace curve", case_pace_curve),
        ("live-day pace + projection", case_pace_projection),
        ("90-day fixture", case_fixture),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
