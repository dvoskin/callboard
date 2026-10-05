""""Collected" must not require "called".

Ana Salazar banks money every day on the shared sheet while her phone line
moved to Inbound (ext 271) in August. The billing board was keyed on the CALL
roster, so her tab never matched and her takings were invisible -- not zero,
invisible, which is worse because nothing said so.

Two separate defects, so two separate claims here:
  1. The tab "Ana" maps to a person at all (roster-derived matching needs four
     letters, so a three-letter first name can only ever match explicitly).
  2. The board SHOWS someone who collected without calls, without ranking them
     on talk time they do not have.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod          # noqa: E402
import collections_client as C  # noqa: E402


def _eq(label, got, want, fails):
    ok = got == want
    print("  %-52s %s (got %r)" % (label, "ok" if ok else "MISMATCH", got))
    return fails + (0 if ok else 1)


def case_tab_map():
    f = 0
    m = C.build_tab_map(["Vivian Martinez", "Yareth Pavon",
                         "Gabriela Maldonado", "Andrea Pleasant"])
    f = _eq("tab 'Ana' maps to Ana Salazar", m.get("ana"), "Ana Salazar", f)
    f = _eq("a renamed 'Salazar' tab also maps", m.get("salazar"), "Ana Salazar", f)
    # The negative half: the late-fee tabs are NOT a biller's collections.
    # Measured 2026-10-05 the Alex tab totals -$929,709 on a different layout.
    f = _eq("'Alex' is not mapped to an agent", m.get("alex"), None, f)
    f = _eq("'Alex' is a known non-agent tab",
            C._norm_tab("Alex") in C.NON_AGENT_TABS, True, f)
    f = _eq("'CASH' is a known non-agent tab",
            C._norm_tab("CASH") in C.NON_AGENT_TABS, True, f)
    return f


def _build(coll_data, unknown_tabs=()):
    """Drive the real billing report path with a stubbed collections cache."""
    roster, roster_meta = appmod._billing_roster("billing")
    day = "2026-10-01"
    rows_by_agent = {
        seat["name"]: {"rows": [], "ext": seat["ext"], "ext_id": seat["ext_id"],
                       "complete": True, "missing_days": []}
        for seat in roster
    }
    real = appmod._collections.cached
    appmod._collections.cached = lambda: (
        coll_data,
        {"loading": False, "tabs": {}, "errors": [], "age_seconds": 1,
         "unmapped_tabs": list(unknown_tabs),
         "unknown_person_tabs": list(unknown_tabs)},
    )
    try:
        return appmod._v6_finish(rows_by_agent, {"cached": 1, "fetched": 0, "missing": 0},
                                 "billing", roster, roster_meta, day, day, 240, day,
                                 days=[day])
    finally:
        appmod._collections.cached = real


def case_collector_without_calls_is_shown():
    f = 0
    rep = _build({"Ana Salazar": {date(2026, 10, 1): 4200.0}})
    names = [c["name"] for c in rep.get("collectors", [])]
    f = _eq("Ana appears as a collector", names, ["Ana Salazar"], f)
    f = _eq("with her money", (rep["collectors"][0]["collected_total"]
                               if rep.get("collectors") else None), 4200.0, f)
    ranked = [a["name"] for a in rep.get("ranked", [])]
    f = _eq("and is NOT ranked on talk time", "Ana Salazar" in ranked, False, f)

    # Negative half: a roster seat's money must still go through the normal
    # path, not get re-reported as a collector.
    rep2 = _build({"Vivian Martinez": {date(2026, 10, 1): 900.0}})
    f = _eq("a roster seat is not duplicated as a collector",
            [c["name"] for c in rep2.get("collectors", [])], [], f)
    return f


def case_unread_person_tab_warns():
    f = 0
    rep = _build({}, unknown_tabs=["Mariana"])
    kinds = [w["kind"] for w in rep.get("warnings", [])]
    f = _eq("an unread person-tab raises a warning",
            "collections_unread_tab" in kinds, True, f)
    rep2 = _build({})
    f = _eq("no warning when every tab is accounted for",
            "collections_unread_tab" in [w["kind"] for w in rep2.get("warnings", [])],
            False, f)
    return f


def run():
    total = 0
    for title, fn in [
        ("the sheet's tab names map to the right people", case_tab_map),
        ("a collector with no calls is shown, not ranked", case_collector_without_calls_is_shown),
        ("an unread person-tab is reported", case_unread_person_tab_warns),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
