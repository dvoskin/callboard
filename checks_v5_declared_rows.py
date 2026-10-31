"""Every declared sales rep is on /v5 and the /teams Sales Floor even before her
first call of the day (Danny, 2026-10-26: "I don't see Maria Carvajal on the call
tracker"). Run: .venv/bin/python checks_v5_declared_rows.py"""
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

declared = [s["name"] for s in A._TEAM_ROSTERS["sales"] if s.get("always_rank")]
ck("the sales roster still declares its reps always_rank (the flag this feature reads)", "Maria Carvajal" in declared and len(declared) >= 12, declared)   # 14 after Gregory/Wellington moved and Ramon quit, 2026-10-30
report = {"ranked": [{"name": "Maia Pasifae Palma", "talk": 300, "attempts": 4}], "unranked": [{"name": "charlotte mckay", "talk": 0}]}
books = {"maria carvajal": {"quotes_sent": 2, "quotes_invoiced": 1, "retainers_sent": 0, "retainers_paid": 1, "paid_amount": 2725.0, "display": "Maria Carvajal"}}
added = A._v5_declared_rows(report, books)
names = [a["name"] for a in report["unranked"]]
ck("Maria Carvajal is added at zero calls", "Maria Carvajal" in added and "Maria Carvajal" in names, (added, names))
ck("...with her real Books figures beside the zero", next(a for a in report["unranked"] if a["name"] == "Maria Carvajal")["books"]["paid_amount"] == 2725.0, report["unranked"])
ck("every other declared rep without a row is added too, once", sorted(added) == sorted(n for n in declared if n not in ("Maia Pasifae Palma", "Charlotte McKay")), sorted(added))
ck("a rep already on the board is not doubled, whatever the case of her name", names.count("charlotte mckay") == 1 and "Charlotte McKay" not in names and "Maia Pasifae Palma" not in names, names)
ck("a departed rep is never added back", "Seydi Medrano" not in names, names)
ck("the zero rows are unranked (they cannot move the floor)", "Maria Carvajal" not in [a["name"] for a in report["ranked"]], report["ranked"])
again = A._v5_declared_rows(report, books)
ck("a second pass adds nobody", again == [], again)
for e in errors: print("  FAIL " + e)
print("%d passed, %d failed" % (passed, len(errors)))
sys.exit(1 if errors else 0)
