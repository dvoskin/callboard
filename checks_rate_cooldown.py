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

    # ---- PRESENCE ---------------------------------------------------------
    # detailedTelephonyState presence is a HEAVY call, same budget as the
    # account call-log. It sat outside the cooldown in BOTH directions: it never
    # reported its own 429, so the other callers were never told to stand down,
    # and it never checked, so it kept spending during a cooldown they were
    # honouring. Worse, its 60s cache expiry is only set on SUCCESS, so while
    # rate limited it retried on every /v3 page load -- a hot loop of Heavy
    # requests against the budget the KPI boards need.
    import ringcx_client as _rcmod

    class _R429:
        status_code = 429
        headers = {"Retry-After": "60"}
        def raise_for_status(self): raise AssertionError("should not reach here")
        def json(self): return {}

    c5 = RingCXClient()
    c5._ensure_rc_token = lambda: None
    c5._rc_headers = lambda: {}
    c5._fetch_extension_names = lambda: {}
    hits = {"n": 0}

    def _get429(url, **kw):
        hits["n"] += 1
        return _R429()

    real_get = _rcmod.requests.get
    _rcmod.requests.get = _get429
    try:
        out = c5.get_agent_statuses()
        cases += [
            ("presence 429 -> no agents", out, []),
            ("...opens the shared cooldown", c5.rate_limited(), True),
            ("...and says it was refused",
             "429" in (c5.last_presence_note or ""), True),
        ]
        # Now cooling: a second call must spend NOTHING.
        before = hits["n"]
        c5.get_agent_statuses()
        cases.append(("cooling presence costs nothing", hits["n"], before))
    finally:
        _rcmod.requests.get = real_get

    # A refusal must not wipe a populated cache -- "we were refused" and "the
    # floor is quiet" must not share a shape.
    c6 = RingCXClient()
    c6._ensure_rc_token = lambda: None
    c6._rc_headers = lambda: {}
    c6._fetch_extension_names = lambda: {}
    c6._agents_cache = [{"name": "Someone", "telephony_status": "CallConnected"}]
    c6._agents_cache_expiry = 0.0          # stale, so it will try to refresh
    _rcmod.requests.get = _get429
    try:
        kept = c6.get_agent_statuses()
    finally:
        _rcmod.requests.get = real_get
    cases.append(("refusal keeps the last good list", len(kept), 1))

    # A 429 must not be retried into the cooldown it just opened. RingEX names a
    # 60s window; a web request can spare 2s. Sleeping 2s and asking again spends
    # another Heavy request on an answer already known, and holds the worker
    # while it does it.
    c7 = RingCXClient()
    c7._ensure_rc_token = lambda: None
    c7._rc_headers = lambda: {}
    tries = {"n": 0}

    class _R429b:
        status_code = 429
        headers = {"Retry-After": "60"}
        text = "Request rate exceeded"
        ok = False

    def _get429b(url, **kw):
        tries["n"] += 1
        return _R429b()

    real_get2 = _rcmod.requests.get
    _rcmod.requests.get = _get429b
    t0 = time.time()
    try:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        end = _dt.now(_tz.utc)
        rows7, meta7 = c7.fetch_extension_calls(1, end - _td(days=1), end, max_wait=2.0)
    finally:
        _rcmod.requests.get = real_get2
    elapsed = time.time() - t0
    cases += [
        ("one request, not a retry", tries["n"], 1),
        ("...and no 2s sleep", elapsed < 1.0, True),
        ("...reported as 429", meta7.get("http_error"), 429),
        ("...with no rows invented", rows7, []),
        ("...cooldown left open", c7.rate_limited(), True),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-32s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
