"""The Sales board must rank sales reps, not everyone who dials a campaign.

The sales roster is derived from behaviour: whoever dials a RingCX campaign in
the last fortnight of reports. That test was never as narrow as it read. The
inbox held exactly one report -- the sales export -- so "dials a campaign"
silently meant "dials a campaign AND is in the sales report". Adding the Inbound
& Scheduling export widened the INPUT, and the predicate widened with it without
a line of code changing: Ariel Ramirez, Johana Duron and Antonio Hernandez
appeared on the Sales Talk Time board because they genuinely do dial RingCX
campaigns. That is their job.

Both directions matter, and the second is the one that bites later:

  1. someone ranked on another team's KPI board never lands on the sales board
  2. a real sales rep is still admitted by exactly the same evidence

A fix that only satisfied (1) could be "exclude everybody", and it would pass a
one-sided check while emptying the board.

Run with no arguments.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

SALES = "Demmi Valladares"          # dials campaigns, on no other board
INBOUND = "Ariel Ramirez"           # dials campaigns, ranked on /customer-service
SCHED = "Sarahi Rivera"             # dials campaigns, ranked on /scheduling
BILLING = "Yareth Pavon"            # RingEX; here to prove the rule is general


def _roster_from(names):
    """_sales_roster() over a synthetic inbox holding exactly these diallers."""
    rows = [{"agent_name": n, "campaign_name": "Some Campaign"} for n in names]
    real_days, real_load = appmod._inbox_days, appmod._load_inbox_csv
    appmod._inbox_days = lambda: ["2026-07-29"]
    appmod._load_inbox_csv = lambda day, is_today=False: (rows, {})
    appmod._v5_roster_cache.update({"at": 0.0, "names": None})
    try:
        return appmod._sales_roster()
    finally:
        appmod._inbox_days, appmod._load_inbox_csv = real_days, real_load
        appmod._v5_roster_cache.update({"at": 0.0, "names": None})


def run():
    fails = 0
    names, meta = _roster_from([SALES, INBOUND, SCHED, BILLING])
    names = names or set()

    cases = [
        ("sales rep admitted", appmod._norm_name(SALES) in names, True),
        ("inbound agent excluded", appmod._norm_name(INBOUND) in names, False),
        ("scheduler excluded", appmod._norm_name(SCHED) in names, False),
        ("billing agent excluded", appmod._norm_name(BILLING) in names, False),
        ("roster is not empty", len(names), 1),
        ("removals are reported", len(meta.get("excluded_other_teams") or []), 3),
    ]

    # A roster of only non-sales diallers must not silently empty the board by a
    # different route: it should still exclude them and say so, not fall back to
    # "filter nobody" and put the whole contact centre back on the board.
    names2, meta2 = _roster_from([INBOUND, SCHED])
    cases += [
        ("all-excluded -> no roster", names2, None),
        ("...and it says why", bool(meta2.get("error")), True),
    ]

    # And end to end. This must be fed REAL rows: build_report([], []) ranks
    # nobody, so "the inbound agent is not ranked" would pass with the roster
    # removed entirely -- a check that cannot fail is not a check.
    from v5_report import build_report
    ex = []
    for n in (SALES, INBOUND):
        for i in range(6):
            ex.append({"agent_name": n, "direction": "Outbound",
                       "result": "Call connected", "duration": 300,
                       "campaign_name": "Some Campaign",
                       "startTime": "2026-07-29T15:0%d:00.000Z" % i})
    both = {a["name"] for a in build_report(ex, []).get("ranked", [])}
    filt = build_report(ex, [], roster={appmod._norm_name(SALES)})
    kept = {a["name"] for a in filt.get("ranked", [])}
    cases += [
        ("unfiltered ranks both", {SALES, INBOUND} <= both, True),
        ("roster drops the inbound one", INBOUND in kept, False),
        ("roster keeps the sales one", SALES in kept, True),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-30s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
