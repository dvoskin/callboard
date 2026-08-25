"""A collections tab must not drop out of the board in silence.

The sheet's tab for Andrea Pleasant is called "PLEASANT". The map knew
"andrea" and "a.pleasant", so it matched nothing and her collected amounts
simply were not there -- including 2026-08-25, a day she had logged $8,006.
Nothing errored, nothing warned, the column just read as if she collected
nothing. That is the failure mode this codebase keeps meeting: an absence
rendered as a zero.

Two halves, and the second is the one that lasts:

  1. a tab named after a rostered agent matches, whatever else it is called
  2. a tab that matches NOTHING is reported, because only a person can tell a
     deliberate CASH/template tab from an agent quietly missing

Run with no arguments. Reads nothing from the network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections_client import build_tab_map, _norm_tab  # noqa: E402

ROSTER = ["Vivian Martinez", "Yareth Pavon", "Gabriela Maldonado", "Andrea Pleasant"]

# Exactly what the live sheet holds, tab names verbatim.
LIVE_TABS = ["CASH", "Vivian", "Alex", "Alex (Late fee)", "Yareth",
             "Gabriela", "PLEASANT", "Late fee", "NEW TRACKER template"]


def run():
    m = build_tab_map(ROSTER)
    matched = {t: m.get(_norm_tab(t)) for t in LIVE_TABS}
    unmapped = sorted(t for t, a in matched.items() if a is None)

    fails = 0
    cases = [
        ("PLEASANT -> Andrea", matched["PLEASANT"], "Andrea Pleasant"),
        ("Vivian matches", matched["Vivian"], "Vivian Martinez"),
        ("Yareth matches", matched["Yareth"], "Yareth Pavon"),
        ("Gabriela matches", matched["Gabriela"], "Gabriela Maldonado"),
        ("every seat is covered",
         len({a for a in matched.values() if a}), len(ROSTER)),
        # The negative half. A map loose enough to catch "PLEASANT" must not
        # start folding CASH or a template into somebody's takings.
        ("CASH is not an agent", matched["CASH"], None),
        ("Late fee is not an agent", matched["Late fee"], None),
        ("template is not an agent", matched["NEW TRACKER template"], None),
        ("Alex is not on this roster", matched["Alex"], None),
        ("unmatched tabs are named", unmapped,
         ["Alex", "Alex (Late fee)", "CASH", "Late fee", "NEW TRACKER template"]),
    ]

    # Short fragments must not match: an agent called "Ana" would otherwise
    # claim a tab named "ANALYSIS", and initials would claim half the sheet.
    m2 = build_tab_map(["Ana Salazar", "Bo Li"])
    cases += [
        ("short names do not match", m2.get(_norm_tab("Ana")), None),
        ("...nor two-letter ones", m2.get(_norm_tab("Li")), None),
        ("...but the full surname does", m2.get(_norm_tab("Salazar")), "Ana Salazar"),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-30s want %-46s got %-46s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
