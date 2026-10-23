"""/api/v6/cx-rows: one row per delivered RingCX interaction, deduped across report scopes, behind the ingest key.

Two scopes on one day carry the same call; the export must return it once. A day with no
report must be listed as missing, not silently skipped. Without the key the rows (patient
numbers) must not come back. Reads nothing from the network.
"""
import os, sys, tempfile, pathlib
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["TZ_OFFSET_HOURS"] = "-4"
os.environ["INGEST_API_KEY"] = "test-ingest-key"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A  # noqa: E402
A.INGEST_API_KEY = "test-ingest-key"
inbox = pathlib.Path(tempfile.mkdtemp()) / "ringcx_inbox"; inbox.mkdir(parents=True)
A.RINGCX_INBOX_DIR = inbox; A._inbox_parse_cache.clear()
HEAD = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,Talk Time (min),Sum of Interaction Duration,"
        "Call Type,Call Result,Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)\n")
ROW_A = "2026-10-20,Judith Merlo,10:15:00,Voice,Holdings Journeys,95,120,Outbound,Outbound Answered,9175551234,9295550000,Reached,10\n"
ROW_B = "2026-10-20,Alex Morales,11:00:00,Voice,Holdings Journeys,0,30,Outbound,No Answer,9175559999,9295550000,No Answer,5\n"
(inbox / "interactions_2026-10-20.csv").write_text(HEAD + ROW_A + ROW_B)
(inbox / "interactions_2026-10-20__interaction_report_inbound_scheduling.csv").write_text(HEAD + ROW_A)
errors = []
def ck(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok: errors.append(name)
c = A.app.test_client()
ck("no key -> 401", c.get("/api/v6/cx-rows?start=2026-10-20").status_code == 401)
ck("wrong key -> 401", c.get("/api/v6/cx-rows?start=2026-10-20", headers={"X-API-Key": "nope"}).status_code == 401)
r = c.get("/api/v6/cx-rows?start=2026-10-19&end=2026-10-20", headers={"X-API-Key": "test-ingest-key"})
j = r.get_json() or {}
ck("200 with key", r.status_code == 200)
ck("same call in two scopes returned once", j.get("count") == 2)
ck("patient number kept", sorted(x.get("ani") for x in j.get("rows", [])) == ["9175551234", "9175559999"])
ck("empty day listed as missing", j.get("days_missing") == ["2026-10-19"] and j.get("days_with_data") == ["2026-10-20"])
ck("start_iso filled", all(x.get("start_iso") for x in j.get("rows", [])))
ck("bad range -> 400", c.get("/api/v6/cx-rows?start=2026-10-20&end=2026-08-01", headers={"X-API-Key": "test-ingest-key"}).status_code == 400)
ck("over 62 days -> 400", c.get("/api/v6/cx-rows?start=2026-01-01&end=2026-10-20", headers={"X-API-Key": "test-ingest-key"}).status_code == 400)
print("FAILED" if errors else "ALL PASSED"); sys.exit(1 if errors else 0)
