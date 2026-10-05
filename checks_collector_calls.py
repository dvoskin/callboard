"""A collector who works another line gets BOTH halves of her day shown.

The case was Ana Salazar: she collected on the billing sheet and took her calls
in RingCX (313 interactions in 14 days, roster "inbound"), so her money was on
the billing board and her calls on the inbound one and neither screen showed a
whole person. She has since become a billing seat read from RingCX, which is a
better answer for her -- see checks_seat_source_split.

The exemplar here is therefore SYNTHETIC on purpose. Pinning this mechanism to a
real person meant that promoting her broke eleven assertions about machinery that
had not changed, and the next collector who turns up off-roster needs it working.

Two things this has to keep straight, because getting either wrong turns a
real number into a wrong one:
  * her talk time obeys the same connected-only rule as everyone else -- RingEX
    reports a nonzero duration on calls nobody answered, and RingCX rows are
    translated into that same shape, so ring time must not become talk time;
  * she is NOT ranked. Billing's targets are outbound-billing targets
    (talk 60/85/110), hers are inbound's (80/133/180). A real figure measured
    against the wrong bar is the same defect as pacing a team on another
    team's curve.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

DAY = "2026-10-01"


def _cx(direction, result, dur):
    return {"direction": direction, "result": result, "duration": dur,
            "start_time": DAY + "T15:00:00-04:00"}


def _eq(label, got, want, fails):
    ok = got == want
    print("  %-54s %s (got %r)" % (label, "ok" if ok else "MISMATCH", got))
    return fails + (0 if ok else 1)


def _build(coll_data, cx_rows, raise_cx=False):
    roster, roster_meta = appmod._billing_roster("billing")
    rows_by_agent = {
        seat["name"]: {"rows": [], "ext": seat["ext"], "ext_id": seat["ext_id"],
                       "complete": True, "missing_days": []}
        for seat in roster
    }
    real_coll = appmod._collections.cached
    real_cx = appmod._v6_cx_rows_for_team
    appmod._collections.cached = lambda: (
        coll_data, {"loading": False, "tabs": {}, "errors": [], "age_seconds": 1,
                    "unmapped_tabs": [], "unknown_person_tabs": []})

    # Calls only attach to a collector who holds a seat on a RingCX team, so the
    # synthetic collector gets a synthetic seat. Patched, not added to the real
    # roster, so this check cannot be passed or broken by a roster edit.
    real_rosters = appmod._TEAM_ROSTERS
    appmod._TEAM_ROSTERS = dict(
        real_rosters,
        inbound=list(real_rosters["inbound"]) + [
            {"name": "Marisol Vega", "ext_id": 999000111, "ext": "299"}])

    def _fake_cx(team, days, roster_):
        if raise_cx:
            raise RuntimeError("inbox unreadable")
        return (cx_rows, len(days), {})
    appmod._v6_cx_rows_for_team = _fake_cx
    try:
        return appmod._v6_finish(rows_by_agent, {"cached": 1, "fetched": 0, "missing": 0},
                                 "billing", roster, roster_meta, DAY, DAY, 240, DAY,
                                 days=[DAY])
    finally:
        appmod._collections.cached = real_coll
        appmod._v6_cx_rows_for_team = real_cx
        appmod._TEAM_ROSTERS = real_rosters


def _off_roster(rep):
    return next((c for c in rep.get("collectors", []) if c["name"] == "Marisol Vega"), None)


def case_calls_shown_beside_money():
    f = 0
    rows = {"Marisol Vega": [
        _cx("Outbound", "Call connected", 300),   # 5 min talk
        _cx("Inbound", "Accepted", 120),          # 2 min talk
        _cx("Outbound", "No Answer", 80),         # ring time, NOT talk
        _cx("Inbound", "Missed", 40),             # never picked up
    ]}
    rep = _build({"Marisol Vega": {date(2026, 10, 1): 9100.0}}, rows)
    a = _off_roster(rep)
    f = _eq("the off-roster collector is listed", bool(a), True, f)
    f = _eq("her money is still there", a and a["collected_total"], 9100.0, f)
    f = _eq("her calls are attached", a and a["calls_tracked"], True, f)
    f = _eq("the platform is named", a and a["call_source"], "RingCX", f)
    # handled = outbound dials + inbound ANSWERED. The missed inbound is not hers.
    f = _eq("handled calls = 2 out + 1 answered in", a and a["calls"], 3, f)
    f = _eq("connected counts only the two that spoke", a and a["connected"], 2, f)
    # 300 + 120 = 420s. The 80s ring and 40s miss must NOT appear.
    f = _eq("talk is connected-only (ring time excluded)", a and a["talk_minutes"], 7.0, f)
    f = _eq("still NOT ranked",
            "Marisol Vega" in [x["name"] for x in rep.get("ranked", [])], False, f)
    return f


def case_no_report_is_not_zero_calls():
    """On RingCX, but nothing delivered for the window: unknown, not zero."""
    f = 0
    rep = _build({"Marisol Vega": {date(2026, 10, 1): 9100.0}}, {})
    a = _off_roster(rep)
    f = _eq("she still appears for the money", bool(a), True, f)
    # False, not absent: the collector is built with calls_tracked=False and it
    # only flips true when rows actually arrived. The template reads the flag,
    # so "no rows" and "rows not delivered yet" must both leave it false rather
    # than one of them quietly rendering a zero.
    f = _eq("calls are NOT claimed as tracked", a and a.get("calls_tracked"), False, f)
    f = _eq("no invented zero call count", a and a.get("calls"), None, f)
    f = _eq("but the platform is still named", a and a.get("call_source"), "RingCX", f)

    # And a broken inbox must not take the board down with it.
    rep2 = _build({"Marisol Vega": {date(2026, 10, 1): 50.0}}, {}, raise_cx=True)
    f = _eq("an unreadable inbox still renders the money",
            (_off_roster(rep2) or {}).get("collected_total"), 50.0, f)
    return f


def case_collector_with_no_cx_seat():
    """Someone who collects and is on no RingCX roster gets no call fields."""
    f = 0
    rep = _build({"Someone Else": {date(2026, 10, 1): 400.0}},
                 {"Someone Else": [_cx("Outbound", "Call connected", 60)]})
    c = next((x for x in rep.get("collectors", []) if x["name"] == "Someone Else"), None)
    f = _eq("they appear for the money", bool(c), True, f)
    f = _eq("no call source invented", c and c.get("call_source"), None, f)
    f = _eq("no calls attached", c and c.get("calls_tracked"), False, f)
    return f


def run():
    total = 0
    for title, fn in [
        ("calls sit beside the money, connected-only", case_calls_shown_beside_money),
        ("no delivered report is unknown, not zero calls", case_no_report_is_not_zero_calls),
        ("a collector with no RingCX seat gets no calls", case_collector_with_no_cx_seat),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
