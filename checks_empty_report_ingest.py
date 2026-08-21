"""Regression: a valid report with no interactions is not a wrong file.

Every RingCX report between midnight and the first dial of the day parses to
zero rows. The ingest refused all of them with 400 wrong_file, so the Apps Script
forwarder -- which marks a message seen only on HTTP 200 -- never marked them,
re-posted ~33 of them every five minutes, and reported every run as Failed. A
quiet night and a broken pipeline became indistinguishable, and the run's own
error text blamed an API key that was fine.

The header check already rejects anything that is not an Interaction Report, so
the zero-row branch only ever fires on a REAL report. It is now EmptyReportError
(a CsvShapeError subclass, so every other catcher is unchanged) and the ingest
answers 200 no-op.

The safety property the old guard existed for must still hold: an empty report
must never replace a populated day.
"""
import os, sys, tempfile, io as _io

HERE = __file__.rsplit('/', 1)[0]
sys.path.insert(0, HERE)

INBOX = tempfile.mkdtemp(prefix="inbox_")
os.environ["INGEST_API_KEY"] = "test-key"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="data_")

import app as A
from pathlib import Path
A.RINGCX_INBOX_DIR = Path(INBOX)
A.INGEST_API_KEY = "test-key"
A.app.config["TESTING"] = True
client = A.app.test_client()

HDR = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
       "Call Type,Call Result,Talk Time (min),Sum of Interaction Duration,"
       "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)")

def row(date, agent, t, talk=120):
    return ("%s,%s,%s ,Voice Call,Outbound Campaign,Outbound,Connected,%d,%d,"
            "'+15551112222,'+15553334444,Interested,10" % (date, agent, t, talk, talk))

EMPTY    = HDR + "\n"
EMPTY_AVG= HDR + "\nAverage,,,,,,,0,0,,,,\n"
FULL     = HDR + "\n" + row("08/21/2026", "Charlotte McKay", "09:15:00") + "\n" \
                      + row("08/21/2026", "Adriana Gentry", "09:40:00") + "\n"
LATER    = HDR + "\n" + row("08/21/2026", "Charlotte McKay", "09:15:00") + "\n" \
                      + row("08/21/2026", "Adriana Gentry", "09:40:00") + "\n" \
                      + row("08/21/2026", "Mabel Alvarez",  "11:05:00") + "\n"
NOT_A_REPORT = "Invoice Number,Customer,Total\nINV-1,Acme,500\n"

def post(body):
    return client.post("/api/v5/ingest", headers={"X-API-Key": "test-key"},
                       data=body.encode(), content_type="text/csv")

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:220]) if not cond else ""))
    if not cond: fail.append(name)

day = Path(INBOX) / "interactions_2026-08-21.csv"

# 1. an empty report is accepted as a no-op, and writes nothing
r = post(EMPTY)
ok("empty report -> 200", r.status_code == 200, r.status_code)
ok("empty report -> status empty_report", r.get_json().get("status") == "empty_report", r.get_json())
ok("empty report stored nothing", not day.exists(), list(Path(INBOX).iterdir()))

r = post(EMPTY_AVG)
ok("empty report with only an Average row -> 200", r.status_code == 200, r.get_json())

# 2. a file that is not an Interaction Report is STILL rejected loudly
r = post(NOT_A_REPORT)
ok("wrong file -> 400", r.status_code == 400, r.status_code)
ok("wrong file -> wrong_file", r.get_json().get("error") == "wrong_file", r.get_json())

# 3. a populated report files normally
r = post(FULL)
ok("populated report -> 200", r.status_code == 200, r.get_json())
ok("populated report wrote the day", day.exists(), list(Path(INBOX).iterdir()))
before = day.read_text() if day.exists() else ""
ok("day holds both agents", "Charlotte McKay" in before and "Adriana Gentry" in before)

# 4. THE SAFETY PROPERTY: an empty report must not erase a populated day
r = post(EMPTY)
ok("empty after populated -> 200", r.status_code == 200, r.get_json())
after = day.read_text() if day.exists() else ""
ok("populated day survives an empty report", after == before and "Charlotte McKay" in after,
   "file changed" if after != before else "file gone")

# 5. a later report still replaces it (watermark path untouched)
r = post(LATER)
after2 = day.read_text() if day.exists() else ""
ok("later report still replaces the day", "Mabel Alvarez" in after2, r.get_json())

# 6. an earlier report still cannot roll the day back
r = post(FULL)
after3 = day.read_text() if day.exists() else ""
ok("earlier report cannot roll the day back", "Mabel Alvarez" in after3, r.get_json())

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
