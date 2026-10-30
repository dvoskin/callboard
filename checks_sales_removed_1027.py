"""Claudia Villalobo, Duany Fermin and Cristina Cots are off the sales rosters
(Danny, 2026-10-27: "Remove Claudia, Duany, and Cristina Cots from sales rosters").
Off the declared roster, kept off /v5 by the departed list (Duany was never
declared: /v5 admits any campaign dialler on no other board), no zero rows for
them -- and their RingEX lines still read for STOP replies.
Run: .venv/bin/python checks_sales_removed_1027.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
import app as A  # noqa: E402
passed, errors = 0, []
def ck(label, cond, got=None):
    global passed
    if cond: passed += 1
    else: errors.append("%s -- got %r" % (label, got))

OFF = ["Claudia Villalobo", "Duany Fermin", "Cristina Cots"]
declared = [s["name"] for s in A._TEAM_ROSTERS["sales"]]
ck("none of the three is on the declared sales roster", not [n for n in OFF if n in declared], declared)
# Gregory Beltran and Wellington Santiago moved to the coordinator trainees on 2026-10-30 (checks_trainees.py).
ck("the rest of the floor is untouched", {"Maria Carvajal", "Adelita Flowers", "Annyta Santiago"} <= set(declared), declared)
ck("all three are on the /v5 exclusion list, so a campaign dial cannot put them back",
   {"claudia villalobo", "duany fermin", "cristina cots"} <= A._DEPARTED_SALES_NAMES, A._DEPARTED_SALES_NAMES)
report = {"ranked": [], "unranked": []}
added = A._v5_declared_rows(report, {})
ck("no zero row is added for any of them", not [n for n in OFF if n in added], added)
extra = {s["name"]: s for s in A._SMS_EXTRA_SEATS["sales"]}
ck("their RingEX lines are still in the STOP scan",
   all(n in extra and extra[n].get("ext_id") for n in OFF) and extra["Claudia Villalobo"]["ext"] == "210"
   and extra["Duany Fermin"]["ext"] == "121" and extra["Cristina Cots"]["ext"] == "133", extra.keys())
seats, _ = A._sms_seats("sales")
ck("...and _sms_seats('sales') returns them for the opt-out scan", all(any(s["name"] == n for s in seats) for n in OFF), [s["name"] for s in seats])
for e in errors: print("  FAIL " + e)
print("%d passed, %d failed" % (passed, len(errors)))
sys.exit(1 if errors else 0)
