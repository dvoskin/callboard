"""Books must never be fetched inside a request, and a zero it never sent must
not look like a zero it did.

_v5_books ran in the request path behind a five-minute cache, so one page load
in every five minutes paid the whole cost of several Books document listings.
That was survivable only while the token was refusing instantly. The moment
Books started answering, the sales board went from quick to barely loading --
the fix working made the symptom appear.

Two properties:

  1. the front door never blocks: on a cold cache it returns immediately with
     loading:true and kicks a background refresh
  2. retainers paid is shown only when Books actually ANSWERED. It was hidden
     for exactly this reason while the token was broken: a confident 0 against
     a rep who did collect is worse than no figure at all.

Run with no arguments. Reads nothing from the network.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402


def run():
    fails = 0
    cases = []

    # 1. cold cache must return AT ONCE, and must not have fetched.
    fetched = {"n": 0}
    real = appmod._v5_books_fetch

    def slow_fetch(a, b):
        fetched["n"] += 1
        time.sleep(2.0)          # a Books call people would notice
        return {}, {"cached": False, "errors": []}

    appmod._v5_books_fetch = slow_fetch
    appmod._v5_books_cache.clear()
    appmod._v5_books_inflight.clear()
    try:
        t0 = time.time()
        by_agent, meta = appmod._v5_books("2026-08-26", "2026-08-26")
        elapsed = time.time() - t0
        cases += [
            ("returns immediately", elapsed < 0.5, True),
            ("says it is loading", meta.get("loading"), True),
            ("invents no agents", by_agent, {}),
        ]
        time.sleep(0.2)
        cases.append(("...and kicked a refresh", fetched["n"], 1))
        # a second call while in flight must not start another
        appmod._v5_books("2026-08-26", "2026-08-26")
        time.sleep(0.2)
        cases.append(("no duplicate fetches", fetched["n"], 1))
        time.sleep(2.2)          # let it finish so it clears in-flight
    finally:
        appmod._v5_books_fetch = real
        appmod._v5_books_cache.clear()
        appmod._v5_books_inflight.clear()

    # 2. the board gates the figure on Books having answered.
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "scoreboard_v5.html")).read()
    cases += [
        ("board computes booksOk", "const booksOk" in tpl, True),
        ("...from loading and errors",
         bool(re.search(r"booksOk\s*=\s*!bm\.loading\s*&&\s*!\(bm\.errors", tpl)), True),
        ("retainers paid is rendered", "Retainers paid" in tpl, True),
        ("...only when booksOk",
         bool(re.search(r"booksOk\s*\?\s*cell2\('Retainers paid'", tpl)), True),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-32s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
