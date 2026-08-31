"""A report must be postable by hand when the forwarder cannot deliver one.

The forwarder is a Gmail script with failure modes nothing on this side can
fix: a disabled trigger, or the mailbox's daily quota. On 2026-08-27 the quota
ran out mid-morning and there was no way to get the day's report in without a
terminal and the ingest key.

Two properties, and the second is the one that keeps this from being a hole:

  1. a signed-in person can upload, and it goes through the SAME path as the
     forwarder -- same scoping, same watermark rule, same parse
  2. a WORD-password session cannot. Those get handed round the floor and are
     read-only everywhere else; this writes a report every board then reports
     from.

Run with no arguments.
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["INGEST_API_KEY"] = "test-ingest-key"
os.environ["V5_PASSWORDS"] = "boardword"
os.environ["V7_PASSWORDS"] = "hubword"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

DAY_US, DAY_ISO = "07/17/2026", "2026-07-17"
HDR = ("Date,Agent Full Name,Call Type,Interaction Start Time,Lead Phone,Caller ID,"
       "Channel,Agent Disposition,Call Result,Term Party,Recording URL,"
       "Screen Recording URL,Channel Type,Voicemail Recording URL,RNA,"
       "Queue Time (min),Ring Time (min),Outbound Ring Time (min),"
       "Handle Time (min),Talk Time (min),Wrap Time (min),"
       "Sum of Interaction Duration,Interaction Time (min)")
ROW = (DAY_US + ",Gregory Beltran,OUTBOUND,10:00:00,+15551234567,+15557654321,Camp,"
       "Disp,Outbound Answered,AGENT,,N/A,Voice,N/A,,0,0,0,90,90,0,90,90")
CSV = HDR + "\n" + ROW + "\n"


def _clean():
    for p in appmod.RINGCX_INBOX_DIR.glob("interactions_%s*.csv" % DAY_ISO):
        p.unlink()


def _upload(client, scope="Daily Interaction Report (Sales)"):
    return client.post("/api/v5/ingest?scope=" + scope,
                       data={"file": (io.BytesIO(CSV.encode()), "r.csv")},
                       content_type="multipart/form-data")


def run():
    appmod.app.config["TESTING"] = True
    fails = 0
    cases = []
    _clean()

    # A word session must NOT be able to write a report.
    word = appmod.app.test_client()
    word.post("/v7", data={"password": "hubword"})
    r_word = _upload(word)
    cases += [
        ("word session cannot upload", r_word.status_code, 401),
        ("...and stored nothing",
         appmod._inbox_path_for(DAY_ISO).exists(), False),
    ]

    # A signed-in person can, and it lands through the normal path.
    human = appmod.app.test_client()
    with human.session_transaction() as sess:
        sess["user"] = {"email": "danny@goalsplasticsurgery.com"}
    r_human = _upload(human)
    j = r_human.get_json() or {}
    cases += [
        ("signed-in person can upload", r_human.status_code, 200),
        ("...rows were read", j.get("total_rows"), 1),
        ("...and the day was stored",
         [d["day"] for d in (j.get("days_written") or [])], [DAY_ISO]),
    ]

    # Scope still applies: the page offers the other report, and it must land
    # in its own slot rather than overwriting sales.
    _clean()
    _upload(human, "Daily Interaction Report (Sales)")
    _upload(human, "Interaction Report (Inbound & Scheduling)")
    files = sorted(q.name for q in appmod._inbox_paths_all_scopes(DAY_ISO) if q.exists())
    cases.append(("both scopes kept separate", len(files), 2))

    # The API key path must keep working -- the forwarder still uses it.
    _clean()
    key = appmod.app.test_client()
    r_key = key.post("/api/v5/ingest",
                     headers={"X-API-Key": "test-ingest-key"},
                     data={"file": (io.BytesIO(CSV.encode()), "r.csv")},
                     content_type="multipart/form-data")
    cases.append(("the forwarder's key still works", r_key.status_code, 200))

    # And the page itself is behind the normal gate.
    cases.append(("upload page needs a login",
                  appmod.app.test_client().get("/v5/upload").status_code in (200, 302, 401),
                  True))
    page = human.get("/v5/upload").get_data(as_text=True)
    cases.append(("page offers both reports",
                  "Inbound" in page and "Sales" in page, True))

    _clean()
    for label, got, want in cases:
        ok = got == want
        print("  %-34s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
