"""Books payments beside sheet collections, and never quietly subtracted.

The shared sheet holds what a BILLER keyed in. Customers also pay by
themselves and those never reach it, so the sheet under-states what came in.
Books has the whole till.

They are two different books, though, and that is the whole care here: the
sheet can hold cash Books never sees, and Books holds payments for things
billers do not handle. So the board states both and the gap between them, and
refuses to present a gap as a clean "self-service total" when the arithmetic
would be a fiction.

Four states, because the wrong one silently reading zero is exactly how the
collections column spent a day lying:

  available    both numbers and their difference
  not read     the background refresh has not finished
  refused      Books said no (the missing scope) -- named, not swallowed
  short window the cache holds N days and the window starts before that

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

DAYS = ["2026-08-24", "2026-08-25", "2026-08-26"]


def _report(coll, payments_state):
    roster, roster_meta = appmod._billing_roster("billing")
    real_cached = appmod._collections.cached
    appmod._collections.cached = lambda: (coll, {"cached": True, "tabs": {},
                                                 "errors": [], "unmapped_tabs": []})
    with appmod._payments_lock:
        saved = dict(appmod._payments)
        appmod._payments.update(payments_state)
    try:
        rows = {s["name"]: {"rows": [], "ext": s["ext"], "ext_id": s["ext_id"],
                            "complete": True, "missing_days": []} for s in roster}
        return appmod._v6_finish(rows, {"cached": 0, "fetched": 0, "missing": 0},
                                 "billing", roster, roster_meta,
                                 DAYS[0], DAYS[-1], 240, DAYS[-1], DAYS)
    finally:
        appmod._collections.cached = real_cached
        with appmod._payments_lock:
            appmod._payments.clear()
            appmod._payments.update(saved)


def run():
    from datetime import date
    seat = appmod._TEAM_ROSTERS["billing"][0]["name"]
    coll = {seat: {date(2026, 8, 26): 10000.0}}          # biller recorded 10k
    fails = 0
    cases = []

    # 1. both known: the gap is the part no biller recorded
    rep = _report(coll, {"at": time.time(), "by_day": {"2026-08-26": 13000.0},
                         "error": None, "covers_from": "2026-05-01",
                         "covers_to": "2026-08-26", "count": 9})
    P = (rep.get("collections") or {}).get("payments") or {}
    cases += [
        ("available", P.get("available"), True),
        ("all payments", P.get("all_payments"), 13000.0),
        ("recorded", P.get("recorded_by_billers"), 10000.0),
        ("difference", P.get("difference"), 3000.0),
        ("not flagged backwards", P.get("sheet_exceeds_books"), False),
    ]

    # 2. the gap runs the OTHER way -- the sheet holds money Books does not.
    #    This must not surface as a negative "self-service" figure.
    rep2 = _report(coll, {"at": time.time(), "by_day": {"2026-08-26": 6000.0},
                          "error": None, "covers_from": "2026-05-01",
                          "covers_to": "2026-08-26", "count": 4})
    P2 = (rep2.get("collections") or {}).get("payments") or {}
    cases += [
        ("backwards gap is flagged", P2.get("sheet_exceeds_books"), True),
        ("...and kept signed, not hidden", P2.get("difference"), -4000.0),
    ]

    # 3. refused by Books -- named, and no numbers invented
    rep3 = _report(coll, {"at": time.time(), "by_day": None,
                          "error": "Books customerpayments returned HTTP 401. "
                                   "The Books OAuth token is missing the "
                                   "ZohoBooks.customerpayments.READ scope.",
                          "covers_from": None, "covers_to": None, "count": 0})
    P3 = (rep3.get("collections") or {}).get("payments") or {}
    cases += [
        ("refusal is not available", P3.get("available"), False),
        ("...and says 401", "401" in (P3.get("reason") or ""), True),
        ("...and invents no total", P3.get("all_payments"), None),
        ("...collections still reported",
         (rep3.get("collections") or {}).get("total"), 10000.0),
    ]

    # 4. window starts before the cache -- a total would be short, so none given
    rep4 = _report(coll, {"at": time.time(), "by_day": {"2026-08-26": 13000.0},
                          "error": None, "covers_from": "2026-08-25",
                          "covers_to": "2026-08-26", "count": 9})
    P4 = (rep4.get("collections") or {}).get("payments") or {}
    cases += [
        ("short window refuses", P4.get("available"), False),
        ("...and explains", "before" in (P4.get("reason") or ""), True),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-32s want %-10s got %-10s %s"
              % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
