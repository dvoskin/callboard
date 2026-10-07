"""The ingest request must never wait on RingEX.

_snapshot_ringex runs the ACCOUNT-wide call log: up to ten pages and a
Retry-After wait of up to a minute on a 429. Inside the ingest request, on a
single gunicorn worker with a 90-second timeout, it could outlive the worker;
gunicorn then kills and restarts it, every open request dies with it, and the
Apps Script forwarder that posted the report sees "Address unavailable" for a
server that was busy answering it. Prod restarted on the same commit at
14:59:47 UTC on 2026-10-07, minutes after an ingest landed.

So the snapshot is queued to a thread and the request returns at once. This
check holds the snapshot open and asserts the response does not wait for it,
then releases it and asserts it was actually run -- a snapshot that is never
taken would pass a "returns fast" check just as well.

Run with no arguments. Reads nothing from the network.
"""
import io
import os
import sys
import time
import threading

os.environ["INGEST_API_KEY"] = "check-key"
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

errors, passed = [], 0


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


import tempfile, pathlib  # noqa: E402
import app as A  # noqa: E402

A.RINGCX_INBOX_DIR = pathlib.Path(tempfile.mkdtemp()) / "inbox"
A.RINGCX_INBOX_DIR.mkdir()
A._inbox_parse_cache.clear()

HEAD = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
        "Talk Time (min),Sum of Interaction Duration,Call Type,Call Result,"
        "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)\n")
BODY = HEAD + ("2026-10-07,Jorge Mier,09:01:00,,Inbound,2.0,120,Outbound,Call connected,"
               "5551112222,5553334444,,0\n")

gate = threading.Event()       # the fake snapshot blocks on this
ran = threading.Event()        # ...and signals when it finally runs
seen = {}


def _slow_snapshot(day, tz=None):
    seen["day"], seen["tz"] = day, tz
    gate.wait(5)
    ran.set()
    return {"stored": True}


_real = (A._snapshot_ringex, A._ringex_may_spend)
A._snapshot_ringex = _slow_snapshot
A._ringex_may_spend = lambda: True
A.app.config["TESTING"] = True
try:
    t0 = time.time()
    r = A.app.test_client().post(
        "/api/v5/ingest?tz=240",
        headers={"X-API-Key": "check-key", "X-Report-Scope": "Interaction Report (Inbound & Scheduling)"},
        data={"file": (io.BytesIO(BODY.encode()), "report.csv")},
        content_type="multipart/form-data")
    took = time.time() - t0
    j = r.get_json() or {}
    ck("the report is stored", r.status_code == 200 and j.get("status") == "ok", (r.status_code, j))
    ck("the request does NOT wait for the snapshot", took < 2.0, round(took, 2))
    ck("and says the snapshot is queued, not stored",
       (j.get("ringex_snapshots") or {}).get("2026-10-07", {}).get("queued") is True,
       j.get("ringex_snapshots"))
    ck("the snapshot has not run yet while the request is already answered",
       not ran.is_set(), ran.is_set())
    gate.set()
    ck("the snapshot DOES run, in the background", ran.wait(5), "snapshot never ran")
    ck("for the day that was written, with the request's tz",
       seen.get("day") == "2026-10-07" and seen.get("tz") == 240, seen)
finally:
    gate.set()
    A._snapshot_ringex, A._ringex_may_spend = _real

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
