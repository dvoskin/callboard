"""A per-person day view must separate "report dropped her" from "she stopped".

Asked of Ana Salazar on 2026-10-05: her RingCX calls stop 2026-10-01 while her
collections run to today. A list of last-seen dates cannot say why -- the report
ceasing to carry her and her ceasing to take calls produce the IDENTICAL absence.

Three synthetic days, mirroring the real shape (she drops out, teammates do not):
  D0  today      two scopes, a teammate present, she is ABSENT -> the PERSON
  D1  yesterday  she is present, on two channels, with the teammate
  D2  two ago    nothing delivered at all                      -> the PIPELINE
A diagnostic that cannot tell D0 from D2 is the whole bug this exists to rule
out, so both verdicts are asserted, not just the happy path.

The channel half answers the follow-on question: her queue retired, or her off
it? A teammate still on her channel AFTER her last day means the queue is alive.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
import tempfile
import datetime
import pathlib

# GOOGLE_CLIENT_ID is deliberately NOT set: _v6_allowed() opens the endpoint only
# when there is no Google client configured, so setting one here locks this check
# out with a 401 that looks like a broken endpoint. The auth gate has its own
# checks (checks_billing_password); this one is about the diagnostic's answer.
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["TZ_OFFSET_HOURS"] = "-4"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

errors = []
import app as A  # noqa: E402

# DATA_DIR is NOT consulted: _data_dir is /data or the repo itself, so the inbox
# must be redirected by hand. Writing fixtures into the real ./ringcx_inbox would
# mix synthetic rows into delivered reports.
inbox = pathlib.Path(tempfile.mkdtemp()) / "ringcx_inbox"
inbox.mkdir(parents=True, exist_ok=True)
A.RINGCX_INBOX_DIR = inbox
A._inbox_parse_cache.clear()

tz = -int(os.environ["TZ_OFFSET_HOURS"]) * 60
today = (datetime.datetime.now(datetime.timezone.utc)
         - datetime.timedelta(minutes=tz)).date()
D0 = today.isoformat()
D1 = (today - datetime.timedelta(days=1)).isoformat()
D2 = (today - datetime.timedelta(days=2)).isoformat()

# The real Interaction Report's own columns. A near-miss header is not a thin
# file -- parse_interaction_csv raises and _parse_inbox_cached returns None, so a
# mis-shaped fixture would make every day look undelivered and "pass" the
# pipeline verdict for the wrong reason. ("Agent Name" for "Agent Full Name"
# already produced one false "0 agents" reading here.)
HEAD = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
        "Talk Time (min),Sum of Interaction Duration,Call Type,Call Result,"
        "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)\n")


def rowc(day, name, minute, channel, ctype=""):
    return ("%s,%s,09:%02d:00,%s,%s,2.0,120,Outbound,Call connected,"
            "5551112222,5553334444,,0\n" % (day, name, minute, ctype, channel))


# D0: delivered and healthy, teammate present, Ana absent.
(inbox / ("interactions_%s__inbound.csv" % D0)).write_text(
    HEAD + rowc(D0, "Ariel Ramirez", 1, "Inbound")
    + rowc(D0, "Angi Fuentes", 2, "Inbound"))
(inbox / ("interactions_%s__scheduling.csv" % D0)).write_text(
    HEAD + rowc(D0, "Jorge Mier", 3, "Scheduling"))
# D1: Ana present on two channels, one of which only she uses.
# The UC row is load-bearing. On a "UC Call" the parser BLANKS campaign_name by
# design and keeps the queue only in `channel`, so a view that read the queue off
# campaign_name would report her own direct line as no queue at all -- the same
# defect that once dropped every agent's own line out of their own totals. Without
# a UC row here, reading the wrong field is indistinguishable from reading the
# right one.
(inbox / ("interactions_%s__inbound.csv" % D1)).write_text(
    HEAD + rowc(D1, "Ana Salazar", 4, "Inbound")
    + rowc(D1, "Ana Salazar", 5, "HerOwnQueue")
    + rowc(D1, "Ana Salazar", 7, "UC", ctype="UC Call")
    + rowc(D1, "Ariel Ramirez", 6, "Inbound"))
# D2: nothing delivered.

A.app.config["TESTING"] = True
r = A.app.test_client().get("/api/v6/cx-agents?who=Ana%20Salazar&days=3")
if r.status_code != 200:
    print("  FAIL endpoint did not answer: HTTP %s %s" % (r.status_code, r.data[:300]))
    # run_checks.sh treats a MISSING tally as "did not finish", so it prints even
    # on the bail-out path.
    print("\n1 failed")
    sys.exit(1)

d = r.get_json()
by = {x["day"]: x for x in d.get("days", [])}
passed = 0


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


ck("who is echoed back", d.get("who") == "Ana Salazar", d.get("who"))
ck("three days returned", len(d.get("days", [])) == 3, len(d.get("days", [])))

a0 = by.get(D0, {})
ck("D0 healthy day, none of hers -> the PERSON",
   a0.get("rows") == 3 and a0.get("for_who") == 0, (a0.get("rows"), a0.get("for_who")))
ck("D0 lists both scopes that arrived", len(a0.get("files", [])) == 2, a0.get("files"))
ck("D0 counts distinct agents", a0.get("distinct_agents") == 3, a0.get("distinct_agents"))

a1 = by.get(D1, {})
ck("D1 counts all three of her rows", a1.get("for_who") == 3, a1.get("for_who"))
ck("D1 counts the whole day", a1.get("rows") == 4, a1.get("rows"))
_hers = [f for f in a1.get("files", []) if f.get("for_who")]
ck("D1 attributes her to the right scope",
   len(_hers) == 1 and _hers[0]["file"].endswith("__inbound.csv"), a1.get("files"))

a2 = by.get(D2, {})
ck("D2 nothing delivered -> the PIPELINE",
   a2.get("rows") == 0 and a2.get("files") == [], (a2.get("rows"), a2.get("files")))

# The two causes must be DISTINGUISHABLE; that is the point of the endpoint.
ck("the PERSON and the PIPELINE do not look alike",
   (a0.get("rows"), len(a0.get("files", []))) != (a2.get("rows"), len(a2.get("files", []))),
   "both read as %r rows" % (a2.get("rows"),))

ck("weekday is carried, so a stop can be checked against the calendar",
   a0.get("weekday") == today.strftime("%a"), a0.get("weekday"))

# ---- the channel discriminator ----
chans = {c["channel"]: c for c in d.get("channels", [])}
ck("only HER channels are listed",
   sorted(chans) == ["HerOwnQueue", "Inbound", "UC"], sorted(chans))
ck("her own UC line keeps its queue, not blanked to none",
   "UC" in chans and "(none)" not in chans, sorted(chans))
ck("a channel she never used is absent", "Scheduling" not in chans, sorted(chans))

inb = chans.get("Inbound", {})
ck("her last day on the shared queue is D1", inb.get("her_last_day") == D1, inb)
ck("a teammate is on it AFTER her -> the queue is alive, the change is hers",
   inb.get("others_after_her") is True, inb)
ck("the queue's own last day runs past hers", inb.get("anyone_last_day") == D0, inb)

own = chans.get("HerOwnQueue", {})
ck("she is not counted as her own witness", own.get("others_after_her") is False, own)
ck("a queue only she used ends when she does",
   own.get("anyone_last_day") == D1 and own.get("her_last_day") == D1, own)

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
