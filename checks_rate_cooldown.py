"""One 429 must stop every RingEX caller, not just the one that got refused.

The live logs showed four independent callers -- the board, the snapshot warmer,
the dashboard refresh, and the RingEX snapshot each ingest POST triggers -- all
retrying into an exhausted per-minute budget at once. Each had its own polite
backoff, and each one's backoff was useless because the other three kept asking,
so the window never got a chance to recover and EVERYTHING 429'd, including the
sales dashboard that had worked for months.

Per-caller backoff cannot fix a shared limit. The cooldown has to be shared.

This drives the client directly rather than over the network: the property under
test is "does a refusal reach the other callers", which is state, not traffic.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ringcx_client import RingCXClient  # noqa: E402


class _Resp:
    """A 429 the way requests hands one back."""
    status_code = 429
    ok = False
    text = "Request rate exceeded"

    def __init__(self, retry_after="60"):
        self.headers = {"Retry-After": retry_after}


def run():
    fails = 0
    c = RingCXClient()

    cases = [
        ("starts open", c.rate_limited(), False),
    ]
    c.note_rate_limited(_Resp().headers["Retry-After"])
    cases += [
        ("one 429 closes it", c.rate_limited(), True),
        ("remaining is sane", 50 < c.cooldown_remaining() <= 60, True),
    ]

    # A second refusal must not shorten an existing cooldown.
    before = c.cooldown_remaining()
    c.note_rate_limited("5")
    cases.append(("a shorter 429 cannot shorten it", c.cooldown_remaining() >= before - 1, True))

    # Clamped: RingEX has been seen to send silly values, and a caller must not
    # be parked for an hour by a header.
    c2 = RingCXClient()
    c2.note_rate_limited("99999")
    cases.append(("absurd Retry-After clamped", c2.cooldown_remaining() <= 120, True))
    c3 = RingCXClient()
    c3.note_rate_limited(None)
    cases.append(("missing Retry-After defaults", c3.rate_limited(), True))

    # While closed, a fetch must SKIP rather than spend a request. If it tried,
    # it would need credentials; a clean skip proves it never got that far.
    c4 = RingCXClient()
    c4.note_rate_limited("60")
    from datetime import datetime, timezone, timedelta
    end = datetime.now(timezone.utc)
    rows, meta = c4.fetch_extension_calls(1, end - timedelta(days=1), end)
    cases += [
        ("fetch skips while closed", rows, []),
        ("and says why", "cooldown" in (meta.get("note") or ""), True),
        ("without pretending success", meta.get("http_error"), 429),
    ]

    # And it reopens.
    c4._cool_until = time.time() - 1
    cases.append(("reopens when it lapses", c4.rate_limited(), False))

    for label, got, want in cases:
        ok = got == want
        print("  %-32s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
