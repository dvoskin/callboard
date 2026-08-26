"""A dry collections tab must be flagged, and a typo must not silence it.

The board shows $0 for a seat two ways that mean opposite things: they collected
nothing today, or their tab stopped being readable weeks ago. Only one of those
is about the person, so the board says which.

The trap this pins: "latest entry" was a plain max() over the dates parsed from
a tab, and Vivian's carries a 2026-11-03 -- a typo, months in the future. It
sorts above every real row, so the DRIEST tab on the sheet reported the freshest
data and the warning meant to flag it stayed silent. Her last genuine entry
predates the window by weeks.

Run with no arguments.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

TODAY = date(2026, 8, 26)


def _report(coll):
    """Run the real tail with a controlled collections cache."""
    roster, roster_meta = appmod._billing_roster("billing")
    real = appmod._collections.cached
    appmod._collections.cached = lambda: (coll, {"cached": True, "tabs": {},
                                                 "errors": [], "unmapped_tabs": []})
    try:
        rows = {seat["name"]: {"rows": [], "ext": seat["ext"],
                               "ext_id": seat["ext_id"], "complete": True,
                               "missing_days": []} for seat in roster}
        t = TODAY.isoformat()
        return appmod._v6_finish(rows, {"cached": 0, "fetched": 0, "missing": 0},
                                 "billing", roster, roster_meta, t, t, 240, t, [t])
    finally:
        appmod._collections.cached = real


def _kinds(rep):
    return {w.get("kind") for w in rep.get("warnings", [])}


def run():
    roster, _ = appmod._billing_roster("billing")
    names = [s["name"] for s in roster]
    fresh, dry, typo = names[0], names[1], names[2]

    coll = {
        fresh: {TODAY: 5000.0},
        dry: {TODAY - timedelta(days=45): 9000.0},
        # A real history that stopped weeks ago, plus a date months ahead.
        typo: {TODAY - timedelta(days=47): 7000.0, date(2026, 11, 3): 1200.0},
    }
    rep = _report(coll)
    kinds = _kinds(rep)
    stale = next((w for w in rep["warnings"]
                  if w.get("kind") == "collections_stale_tab"), None)
    stale_text = (stale or {}).get("message", "")
    latest = (rep.get("collections_meta") or {}).get("latest_by_agent") or {}

    fails = 0
    cases = [
        ("a dry tab is flagged", "collections_stale_tab" in kinds, True),
        ("...naming the dry seat", dry in stale_text, True),
        ("...and the typo seat too", typo in stale_text, True),
        ("the fresh seat is not named", fresh in stale_text, False),
        ("a seat with nothing is flagged",
         "collections_missing_tab" in kinds, True if len(names) > 3 else False),
        # The heart of it: the future date must not be reported as "latest".
        ("future date is not the latest",
         latest.get(typo), (TODAY - timedelta(days=47)).isoformat()),
        ("the fresh seat's latest is today",
         latest.get(fresh), TODAY.isoformat()),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-34s want %-14s got %-14s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
