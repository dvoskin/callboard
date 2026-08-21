"""Regression: the live half of the board must not be frozen by the lagging half.

RingEX is a live API; RingCX arrives by email on its own schedule. The RingEX
snapshot was taken only when a RingCX report was ingested, so when RingCX went
quiet for 20 minutes the live source froze with it -- 148 calls, fetched 11:32,
still 11:32 at 11:46 even though the API would have answered instantly.

It now refreshes on its own clock. The rate limiter is the point: the 429 earlier
in this project came from fetching RingEX on every page load, so a refresh must be
claimed at most once per interval for the whole instance, and a FAILED attempt
must still consume the interval -- otherwise a broken API is retried on every
request, which is how the 429 happened in the first place.
"""
import os, sys, tempfile, time

sys.path.insert(0, __file__.rsplit('/', 1)[0])
os.environ.setdefault("INGEST_API_KEY", "k")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="data_"))
import app as A

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:200]) if not cond else ""))
    if not cond: fail.append(name)

def reset():
    A._ringex_last_attempt["at"] = 0.0

# 1. a fresh snapshot is never refreshed -- that was the whole 429 problem
reset()
ok("a fresh snapshot does not spend an API call",
   not A._ringex_refresh_due(60), "refreshed a 1-minute-old snapshot")
ok("just under the threshold still does not",
   not A._ringex_refresh_due(A._RINGEX_REFRESH_AFTER - 1), "refreshed too eagerly")

# 2. a stale one does
reset()
ok("a stale snapshot refreshes", A._ringex_refresh_due(A._RINGEX_REFRESH_AFTER + 1))

# 3. ...but only once per interval, however many page loads arrive
reset()
first = A._ringex_refresh_due(3600)
rest = [A._ringex_refresh_due(3600) for _ in range(25)]
ok("the first of a burst refreshes", first)
ok("the other 25 page loads do not", not any(rest),
   "%d of 25 concurrent loads each hit the API" % sum(rest))

# 4. a FAILED attempt still consumes the interval
reset()
A._ringex_refresh_due(3600)          # claimed; pretend the fetch then failed
ok("a failed attempt still holds the interval", not A._ringex_refresh_due(3600),
   "a broken API would be retried on every page load -- the original 429")

# 5. the gap is short enough to be useful and long enough to be safe
ok("refresh interval is minutes, not seconds", A._RINGEX_REFRESH_AFTER >= 120,
   A._RINGEX_REFRESH_AFTER)
ok("refresh interval keeps the board current", A._RINGEX_REFRESH_AFTER <= 600,
   A._RINGEX_REFRESH_AFTER)
ok("the attempt floor is not looser than the refresh age",
   A._RINGEX_MIN_GAP >= A._RINGEX_REFRESH_AFTER * 0.5,
   "min gap %s vs refresh after %s" % (A._RINGEX_MIN_GAP, A._RINGEX_REFRESH_AFTER))

# 6. a past day must never trigger a fetch: its snapshot is final
import inspect
src = inspect.getsource(A.api_v5_report)
ok("only today may refresh", "allow_refresh=(date_start == local_today)" in src,
   "a past day would spend API budget on a snapshot that cannot change")

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
