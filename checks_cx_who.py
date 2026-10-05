"""A per-person day view must separate "report missing her" from "she stopped".

Three synthetic days with known answers:
  D0  two scopes, she is in one          -> rows>0, for_who>0
  D1  two scopes, she is in NEITHER      -> rows>0, for_who=0   (= the person)
  D2  no file delivered at all           -> rows=0, for_who=0   (= the pipeline)
A diagnostic that cannot tell D1 from D2 is the whole bug this endpoint exists
to rule out, so both halves are asserted, not just the happy path.
"""
import os, sys, json, tempfile, datetime, pathlib

TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["V6_TOKEN"] = ""
os.environ["TZ_OFFSET_HOURS"] = "-4"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

errors = []
import app as A

# DATA_DIR is NOT consulted: _data_dir is /data or the repo itself, so the
# inbox must be redirected by hand. Writing fixtures into the real
# ./ringcx_inbox would mix synthetic rows into delivered reports.
inbox = pathlib.Path(TMP) / "ringcx_inbox"
inbox.mkdir(parents=True, exist_ok=True)
A.RINGCX_INBOX_DIR = inbox
A._inbox_parse_cache.clear()
tz = -int(os.environ["TZ_OFFSET_HOURS"]) * 60
today = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=tz)).date()
D0 = today.isoformat()
D1 = (today - datetime.timedelta(days=1)).isoformat()
D2 = (today - datetime.timedelta(days=2)).isoformat()

# The real Interaction Report's own columns. A near-miss header is not a thin
# file -- parse_interaction_csv raises and _parse_inbox_cached returns None, so
# a mis-shaped fixture would make every day look undelivered and the check
# would "pass" the pipeline verdict for the wrong reason.
HEAD = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
        "Talk Time (min),Sum of Interaction Duration,Call Type,Call Result,"
        "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)\n")
def row(name, t):
    return ("%s,%s,09:%02d:00,UC Call,UC,2.0,120,Outbound,Call connected,"
            "5551112222,5553334444,,0\n" % (t[0], name, t[1]))

# D0: she is in the inbound scope, not the scheduling one.
(inbox / ("interactions_%s__inbound.csv" % D0)).write_text(
    HEAD + row("Ana Salazar", (D0, 1)) + row("Ariel Ramirez", (D0, 2)))
(inbox / ("interactions_%s__scheduling.csv" % D0)).write_text(
    HEAD + row("Jorge Mier", (D0, 3)))
# D1: both scopes healthy, she is absent from both.
(inbox / ("interactions_%s__inbound.csv" % D1)).write_text(
    HEAD + row("Ariel Ramirez", (D1, 4)) + row("Angi Fuentes", (D1, 5)))
(inbox / ("interactions_%s__scheduling.csv" % D1)).write_text(
    HEAD + row("Jorge Mier", (D1, 6)))
# D2: nothing delivered.

A.app.config["TESTING"] = True
c = A.app.test_client()
r = c.get("/api/v6/cx-agents?who=Ana%20Salazar&days=3")
if r.status_code != 200:
    errors.append("endpoint did not answer: HTTP %s %s" % (r.status_code, r.data[:300]))
    for e in errors: print("  FAIL", e)
    # run_checks.sh reads a bare "N failed" line and treats its ABSENCE as
    # "did not finish", so the tally has to print even on the bail-out path.
    print("\n%d failed" % len(errors))
    sys.exit(1)

d = r.get_json()
by = {x["day"]: x for x in d.get("days", [])}
passed = 0

def ck(label, cond, got):
    global passed
    if cond: passed += 1
    else: errors.append("%s -- got %s" % (label, got))

ck("who echoed", d.get("who") == "Ana Salazar", d.get("who"))
ck("three days returned", len(d.get("days", [])) == 3, len(d.get("days", [])))

a = by.get(D0, {})
ck("D0 counts her row", a.get("for_who") == 1, a.get("for_who"))
ck("D0 counts the whole day", a.get("rows") == 3, a.get("rows"))
ck("D0 lists both scopes", len(a.get("files", [])) == 2, a.get("files"))
_hers = [f for f in a.get("files", []) if f.get("for_who") == 1]
ck("D0 attributes to the right scope",
   len(_hers) == 1 and _hers[0]["file"].endswith("__inbound.csv"),
   a.get("files"))
ck("D0 distinct agents", a.get("distinct_agents") == 3, a.get("distinct_agents"))

b = by.get(D1, {})
ck("D1 healthy day, none of hers -> the PERSON",
   b.get("rows") == 3 and b.get("for_who") == 0, (b.get("rows"), b.get("for_who")))
ck("D1 still lists the scopes that arrived", len(b.get("files", [])) == 2, b.get("files"))

cc = by.get(D2, {})
ck("D2 nothing delivered -> the PIPELINE",
   cc.get("rows") == 0 and cc.get("files") == [], (cc.get("rows"), cc.get("files")))

# The two causes must be DISTINGUISHABLE, which is the point of the endpoint.
ck("D1 and D2 do not look alike",
   (b.get("rows"), len(b.get("files", []))) != (cc.get("rows"), len(cc.get("files", []))),
   "both read as %s" % (cc.get("rows"),))

# Weekday is carried so "she stopped" can be checked against the calendar.
ck("weekday present", bool(a.get("weekday")), a.get("weekday"))

print("%d passed" % passed)
for e in errors: print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
