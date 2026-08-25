"""A burst of report ingests must not become a burst of RingEX requests.

Danny's question was the right one: we only added RingCX reports, so why did
RINGEX volume go up? Because the ingest endpoint fetched RingEX every time a
report wrote a day, so RingEX demand tracked report arrivals -- and the RingCX
work multiplied arrivals about fourfold (each subject now gets its own file),
then resetAndResend replayed the whole window at once. Nine call-log fetches in
twelve seconds against a ceiling of ten a minute, and everything on the account
429'd, the sales dashboard included.

Four properties, each of which was FALSE in production on 2026-08-25:

  1. a burst of ingests spends at most a couple of RingEX fetches
  2. ...while a day that has NO snapshot still gets its first one, always
  3. the extension-name cache actually hits (it never once did: it was gated on
     an expiry only the RingCX presence path ever set)
  4. a cooled-down fetch costs ZERO http requests -- the cooldown used to sit
     after the 140-extension roster fetch, so standing down still spent a request

Run with no arguments.
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["INGEST_API_KEY"] = "test-ingest-key"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod            # noqa: E402
import ringcx_client            # noqa: E402

DAY_US, DAY_ISO = "07/16/2026", "2026-07-16"
HDR = ("Date,Agent Full Name,Call Type,Interaction Start Time,Lead Phone,Caller ID,"
       "Channel,Agent Disposition,Call Result,Term Party,Recording URL,"
       "Screen Recording URL,Channel Type,Voicemail Recording URL,RNA,"
       "Queue Time (min),Ring Time (min),Outbound Ring Time (min),"
       "Handle Time (min),Talk Time (min),Wrap Time (min),"
       "Sum of Interaction Duration,Interaction Time (min)")


def _csv(agent, hhmmss, talk=90):
    return (HDR + "\n" +
            f"{DAY_US},{agent},OUTBOUND,{hhmmss},+15551234567,+15557654321,Camp,"
            f"Disp,Outbound Answered,AGENT,,N/A,Voice,N/A,,0,0,0,{talk},{talk},0,"
            f"{talk},{talk}\n")


def _post(c, text, subject):
    return c.post("/api/v5/ingest",
                  headers={"X-API-Key": "test-ingest-key", "X-Report-Scope": subject},
                  data={"file": (io.BytesIO(text.encode()), "report.csv")},
                  content_type="multipart/form-data")


def _clean():
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()
    snap = appmod._ringex_snap_path(DAY_ISO)
    if snap.exists():
        snap.unlink()


def case_burst():
    """Twelve ingests, like a resetAndResend pass. Count the RingEX fetches."""
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    _clean()
    appmod._ringex_last_attempt["at"] = 0.0

    calls = {"n": 0}
    real = appmod._ringcx._fetch_ringex_agent_calls

    def counting(start_dt, end_dt):
        calls["n"] += 1
        appmod._ringcx.last_ringex_note = None
        return [{"agent_name": "Yareth Pavon", "direction": "Outbound",
                 "result": "Call connected", "duration": 120,
                 "startTime": "2026-07-16T15:00:00.000Z"}]

    appmod._ringcx._fetch_ringex_agent_calls = counting
    try:
        # Four scopes x three replays: every one of these WRITES (a replay with
        # equal reach and equal row count is not skipped), so every one of them
        # used to fetch.
        for rep in range(3):
            for scope in ("Sales", "Inbound & Scheduling", "Retention", "Billing"):
                _post(c, _csv("Gregory Beltran", "10:0%d:00" % rep),
                      "Daily Interaction Report (%s)" % scope)
        first = calls["n"]
    finally:
        appmod._ringcx._fetch_ringex_agent_calls = real

    fails = 0
    for label, got, want in [
        ("12 ingests, <=2 fetches", first <= 2, True),
        ("but not zero", first >= 1, True),
        ("snapshot on disk", appmod._ringex_snap_path(DAY_ISO).exists(), True),
    ]:
        ok = got == want
        print("  %-34s want %-6s got %-6s %s   (fetches=%d)"
              % (label, want, got, "OK" if ok else "<<< FAIL", first))
        fails += 0 if ok else 1
    _clean()
    return fails


def case_first_snapshot_is_never_skipped():
    """The budget must not open a hole. Nothing else creates these files."""
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    _clean()
    appmod._ringex_last_attempt["at"] = time.time()   # budget CLOSED

    calls = {"n": 0}
    real = appmod._ringcx._fetch_ringex_agent_calls

    def counting(start_dt, end_dt):
        calls["n"] += 1
        appmod._ringcx.last_ringex_note = None
        return [{"agent_name": "Yareth Pavon", "direction": "Outbound",
                 "result": "Call connected", "duration": 60,
                 "startTime": "2026-07-16T15:00:00.000Z"}]

    appmod._ringcx._fetch_ringex_agent_calls = counting
    try:
        _post(c, _csv("Gregory Beltran", "10:00:00"), "Daily Interaction Report (Sales)")
        took_first = calls["n"]
        # Second report, same day, budget still closed -> must NOT re-fetch.
        _post(c, _csv("Ariel Ramirez", "17:00:00"), "Interaction Report (Inbound)")
        after_second = calls["n"]
    finally:
        appmod._ringcx._fetch_ringex_agent_calls = real
        appmod._ringex_last_attempt["at"] = 0.0

    fails = 0
    for label, got, want in [
        ("first snapshot taken anyway", took_first, 1),
        ("second does not re-fetch", after_second, 1),
        ("day has a snapshot", appmod._ringex_snap_path(DAY_ISO).exists(), True),
    ]:
        ok = got == want
        print("  %-34s want %-6s got %-6s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    _clean()
    return fails


def case_extension_names_cached():
    """It claimed to be cached and never was: the expiry it read is only set by
    the RingCX presence path, which the call-log path never calls."""
    cl = ringcx_client.RingCXClient()
    http = {"n": 0}

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"records": [{"id": "1", "name": "A", "extensionNumber": "101"}],
                    "navigation": {}}

    def fake_get(url, **kw):
        http["n"] += 1
        return _R()

    real_get, real_hdrs = ringcx_client.requests.get, cl._rc_headers
    ringcx_client.requests.get = fake_get
    cl._rc_headers = lambda: {}
    try:
        cl._fetch_extension_names()
        cl._fetch_extension_names()
        cl._fetch_extension_names()
        n = http["n"]
    finally:
        ringcx_client.requests.get, cl._rc_headers = real_get, real_hdrs

    ok = n == 1
    print("  %-34s want %-6s got %-6s %s" % ("3 calls -> 1 http fetch", 1, n,
                                             "OK" if ok else "<<< FAIL"))
    return 0 if ok else 1


def case_cooldown_costs_nothing():
    """Standing down used to still spend the 140-extension roster request first."""
    cl = ringcx_client.RingCXClient()
    cl.note_rate_limited("60")
    http = {"n": 0}

    # Auth and headers must SUCCEED here. Without this the token call raises
    # first, no request is attempted for that reason instead of the one under
    # test, and "zero http requests" passes whether or not the cooldown is in
    # the right place -- which is exactly what it did on the first draft. A
    # check that cannot fail is not a check.
    cl._ensure_rc_token = lambda: None
    cl._rc_headers = lambda: {}

    def fake_get(url, **kw):
        http["n"] += 1
        raise AssertionError("spent a request while cooling down: %s" % url)

    real_get = ringcx_client.requests.get
    ringcx_client.requests.get = fake_get
    try:
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc)
        rows = cl._fetch_ringex_agent_calls(end - timedelta(days=1), end)
    finally:
        ringcx_client.requests.get = real_get

    note = getattr(cl, "last_ringex_note", None) or ""
    fails = 0
    for label, got, want in [
        ("zero http requests", http["n"], 0),
        ("returns no rows", rows, []),
        ("and says why", "cooldown" in note, True),
    ]:
        ok = got == want
        print("  %-34s want %-6s got %-6s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def run():
    total = 0
    for title, fn in [
        ("a burst of ingests is not a burst of fetches", case_burst),
        ("the first snapshot is never skipped", case_first_snapshot_is_never_skipped),
        ("the extension-name cache actually hits", case_extension_names_cached),
        ("a cooled-down fetch costs nothing", case_cooldown_costs_nothing),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
