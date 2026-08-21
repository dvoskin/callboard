"""Regression: only sales belong on the sales board, and quoting counts as being there.

Two defects, one rule.

  * Vera Payne was on the board. She has no campaign interaction in any report
    the inbox holds and is not the salesperson on a single estimate. She also had
    12 attempts -- over MIN_CALLS_TO_RANK -- so she sat INSIDE the median that
    everyone else was measured against. Filtering downstream would not have fixed
    that; the roster has to apply before floor, bands and totals are derived.

  * Eight people who had sent quotes that morning were missing entirely, because
    the board is built from RingCX and they had not dialled yet.

Danny's rule is that only sales users send quotes and dial RingCX campaigns.
Neither half alone works: campaign-only drops Maisah Brandon (27 estimates,
barely dials), quotes-only drops a rep having a pure-dialling day. So it is the
union, measured over a window wider than the report's.
"""
import sys
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from v5_report import build_report

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:220]) if not cond else ""))
    if not cond: fail.append(name)

def cx(agent, n, campaign=True, talk=600):
    return [{"agent_name": agent, "talk_time": talk, "duration": talk, "wrap_time": 0,
             "direction": "Outbound", "result": "Connected",
             "start_time": "08/21/2026 09:%02d:00" % (i % 60),
             "ani": "+1555000%04d" % i, "dnis": "+1555111%04d" % i,
             "campaign_name": "Camp" if campaign else "", "queue_name": "" if campaign else "UC",
             "call_type": "Voice" if campaign else "UC Call",
             "agent_disposition": "", "source": "ringcx_csv"} for i in range(n)]

# Vera: 12 direct-only attempts, high talk -- exactly the shape that moved the median.
# Distinct talk per rep so the median genuinely moves when she is removed:
# with her the ranked set is [21600, 7200, 3600, 1200] -> 5400; without, 3600.
CX = (cx("Vera Payne", 12, campaign=False, talk=1800)
      + cx("Adelita Flowers", 12, talk=600)
      + cx("Duany Fermin", 12, talk=300)
      + cx("Charlotte McKay", 12, talk=100))

ROSTER = {"adelita flowers", "duany fermin", "charlotte mckay", "maisah brandon"}

# --- 1. unfiltered: Vera is present AND inside the floor ---------------------
before = build_report([], CX, window={"start": "2026-08-21", "end": "2026-08-21"})
names_before = [a["name"] for a in before["ranked"] + before["unranked"]]
ok("without a roster Vera is on the board", "Vera Payne" in names_before, names_before)
floor_before = before["floor"].get("talk")

# --- 2. filtered ------------------------------------------------------------
after = build_report([], CX, window={"start": "2026-08-21", "end": "2026-08-21"},
                     roster=ROSTER)
names_after = [a["name"] for a in after["ranked"] + after["unranked"]]
ok("Vera Payne is off the board", "Vera Payne" not in names_after, names_after)
ok("real reps stay", {"Adelita Flowers", "Duany Fermin", "Charlotte McKay"} <= set(names_after), names_after)
ok("she is named, not silently dropped",
   after["meta"].get("not_sales") == ["Vera Payne"], after["meta"].get("not_sales"))

# --- 3. the floor no longer counts her --------------------------------------
floor_after = after["floor"].get("talk")
ok("the floor changes once she is out", floor_before != floor_after,
   "before=%s after=%s" % (floor_before, floor_after))
ok("totals exclude her too", after["totals"]["talk"] < before["totals"]["talk"],
   "before=%s after=%s" % (before["totals"]["talk"], after["totals"]["talk"]))

# --- 4. roster=None must filter nobody (Books half unavailable) --------------
none_run = build_report([], CX, window={"start": "2026-08-21", "end": "2026-08-21"},
                        roster=None)
ok("roster=None filters nobody",
   "Vera Payne" in [a["name"] for a in none_run["ranked"] + none_run["unranked"]])

# --- 5. a quote-only rep with zero calls gets a row --------------------------
import os, tempfile
os.environ.setdefault("INGEST_API_KEY", "k")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A
row = A._zero_call_row("Maisah Brandon",
                       {"quotes_sent": 3, "quotes_invoiced": 1, "retainers_sent": 0,
                        "retainers_paid": 0, "paid_amount": 0.0})
ok("zero-call row carries the quotes", row["books"]["quotes_sent"] == 3, row)
ok("zero-call row has no dials", row["attempts"] == 0 and row["talk"] == 0, row)
ok("zero-call row is unrankable", row["band"] == "na", row)
ok("zero-call row has the keys the板 template reads".replace("板",""),
   all(k in row for k in ("over_3", "over_10", "longest", "below")), sorted(row))

# --- 6. _sales_roster itself must fail OPEN, not closed ---------------------
# A campaign-only roster would quietly drop every quote-heavy rep, so when the
# Books half fails the roster must be None (filter nobody) and never an empty
# set (filter everybody). Tested here because passing roster=None straight to
# build_report only checks build_report's contract, not _sales_roster's.
class _Boom:
    def list_sent_estimates(self, *a, **k):
        raise RuntimeError("Books customerpayments returned HTTP 401")

_saved = A._books
try:
    A._books = _Boom()
    A._v5_roster_cache.update({"at": 0.0, "names": None, "why": {}})
    names, meta = A._sales_roster()
    ok("Books failure yields None, not an empty set", names is None, repr(names))
    ok("an empty set would have filtered everyone", names != set(), repr(names))
    ok("the failure is reported, not swallowed", bool(meta.get("error")), meta)
finally:
    A._books = _saved
    A._v5_roster_cache.update({"at": 0.0, "names": None, "why": {}})

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
