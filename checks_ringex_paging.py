"""Regression: the RingEX call-log fetch must return the calls it parsed.

Guards the bug fixed on 2026-08-20: the pagination check read a name (`records`)
that was never bound, raising NameError *after* every record on the page had been
parsed. The function's bare `except Exception` swallowed it and returned [], so
_fetch_ringex_agent_calls silently reported zero RingEX calls from 2026-07-10
(d0a24ce) onward -- 41 days -- while /v2/interactions and /api/agent-analytics
both claimed to cover RingEX.

The failure mode is what makes this worth a test: it was invisible. An empty call
list is indistinguishable from a quiet day, and the exception only reached a log line.
"""
import sys, types
from datetime import datetime, timezone

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import ringcx_client as rc


def _client(page_records):
    class R:
        status_code, ok = 200, True
        def json(self): return {"records": page_records}
    rc.requests = types.SimpleNamespace(get=lambda *a, **k: R(), post=lambda *a, **k: R())
    cls = [v for v in vars(rc).values()
           if isinstance(v, type) and hasattr(v, '_fetch_ringex_agent_calls')][0]
    c = cls.__new__(cls)
    c._ensure_rc_token = lambda: "tok"
    c._rc_headers = lambda: {}
    c._fetch_extension_names = lambda: {}
    c.server_url, c.account_id = "https://x", "~"
    return c


CALLS = [
    {"direction": "Outbound", "duration": 152, "result": "Connected",
     "startTime": "2026-08-20T16:38:07.000Z",
     "from": {"name": "Gregory Beltran", "phoneNumber": "+19294371230"},
     "to": {"phoneNumber": "+16315327365"}, "sessionId": "s1"},
    {"direction": "Inbound", "duration": 498, "result": "Answered",
     "startTime": "2026-08-20T16:36:03.000Z",
     "from": {"phoneNumber": "+13479427761"},
     "to": {"name": "Gabriel Johnson", "phoneNumber": "+19293000194"}, "sessionId": "s2"},
]
WINDOW = (datetime(2026, 8, 20, tzinfo=timezone.utc),
          datetime(2026, 8, 20, 23, 59, tzinfo=timezone.utc))


def test_parsed_calls_are_returned():
    out = _client(CALLS)._fetch_ringex_agent_calls(*WINDOW)
    assert len(out) == 2, f"parsed 2 calls but returned {len(out)} -- calls are being dropped"


def test_calls_are_attributed_to_the_internal_party():
    out = _client(CALLS)._fetch_ringex_agent_calls(*WINDOW)
    assert [c["agent_name"] for c in out] == ["Gregory Beltran", "Gabriel Johnson"]


def test_empty_page_is_not_an_error():
    assert _client([])._fetch_ringex_agent_calls(*WINDOW) == []


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "--", e)
    print("\n%d failed" % fails)
    sys.exit(1 if fails else 0)
