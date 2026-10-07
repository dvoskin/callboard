"""The sales floor has a declared board, built from the ingest it already feeds.

Danny, 2026-10-07: "make sure we have access to all these agent analytics from
existing ingests we've been running all in separate". The fifteen names are the
ones the 15-minute call-performance report carried that day, as RingCX prints
them. Until now the sales floor was only inferred (/v5, campaign dialling).

Run with no arguments. Reads nothing from the network.
"""
import os
import sys

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

errors, passed = [], 0


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


import app as A  # noqa: E402

r, meta = A._billing_roster("sales")
names = [x["name"] for x in r]
ck("fifteen declared sales seats", len(r) == 15, len(r))
ck("they survive the roster rebuild without ext_ids", meta.get("size") == 15 and not meta.get("skipped"), meta)
ck("every seat is a RingCX seat matched by name", all(x.get("source") == "ringcx" for x in r), r[:2])
ck("every seat is listed even at zero", all(x.get("always_rank") for x in r), [x["name"] for x in r if not x.get("always_rank")])
ck("Maia Pasifae Palma, the busiest that day, is on it", "Maia Pasifae Palma" in names, names)
ck("read from RingCX", A._TEAM_SOURCES.get("sales") == "ringcx", A._TEAM_SOURCES.get("sales"))
ck("no SMS on the sales board", A._team_sms_enabled("sales") is False, A._team_sms_enabled("sales"))
ck("it has a label", A.TEAM_LABELS.get("sales") == "Sales", A.TEAM_LABELS.get("sales"))
ck("the sales floor is NOT on the landing link (billing + surgical only)",
   "sales" not in A.BOARD_TEAMS, A.BOARD_TEAMS)

A.app.config["TESTING"] = True
c = A.app.test_client()
ck("/sales serves the board", c.get("/sales").status_code == 200, c.get("/sales").status_code)
ck("pinned to one board", 'var FIXED = "sales"' in c.get("/sales").get_data(as_text=True), "FIXED not sales")
_rc = A._ringcx.active_calls_with_status
A._ringcx.active_calls_with_status = lambda: ([{"agent_name": "Maia Pasifae Palma", "call_state": "ACTIVE"}], {"ok": True, "note": None, "read_at": 0, "http_error": None})
A._cx_active_cache.update(at=0.0, calls=[], meta=None)
try:
    j = c.get("/api/v6/presence?team=sales").get_json() or {}
finally:
    A._ringcx.active_calls_with_status = _rc
    A._cx_active_cache.update(at=0.0, calls=[], meta=None)
by = {x["name"]: x for x in j.get("seats", [])}
ck("sales seats get RingCX call status", by.get("Maia Pasifae Palma", {}).get("on_call") is True, by.get("Maia Pasifae Palma"))
# No rows today and not on a call: not known to be online, so Offline -- not
# Available, which is what every absent seat used to read as.
ck("the rest, with no activity today and no shift on file, read No Activity",
   by.get("Gregory Beltran", {}).get("state") == "no_activity" and by.get("Gregory Beltran", {}).get("label") == "No Activity",
   by.get("Gregory Beltran"))

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
