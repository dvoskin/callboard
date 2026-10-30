"""RingCentral texts in the call & text search (2026-10-30).

Danny: "add the texts sent from ringcentral lines too". sms_index.py keeps which
line texted which number (never the text); the search reads the texts live,
phone-filtered, from just those lines. Checks the index, one loop pass (sync,
truncation, the fill and its page budget) and the search's read.

Reads nothing from the network. Run: ./.venv/bin/python -B checks_sms_index.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sms_index as SI  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


NOW = datetime.now(timezone.utc).replace(microsecond=0)
OURS = "+19292647756"
PAT = "+15613889422"

# ---- the index alone -------------------------------------------------------
tmp = tempfile.mkdtemp()
ix = SI.SmsIndex(os.path.join(tmp, "a.sqlite"))
ix.set_lines({"111": {"name": "Ariel Voskin", "ext_no": "689", "numbers": [OURS]},
              "222": {"name": "Judith Merlo", "ext_no": "185", "numbers": ["+19294150000"]}})
n = ix.record("111", [
    {"direction": "Outbound", "from_number": OURS, "to_number": PAT, "start_time": iso(NOW - timedelta(days=3))},
    {"direction": "Inbound", "from_number": PAT, "to_number": OURS, "start_time": iso(NOW - timedelta(days=1))},
    {"direction": "Inbound", "from_number": OURS, "to_number": OURS, "start_time": iso(NOW)},      # our own number
    {"direction": "Outbound", "from_number": OURS, "to_number": "411", "start_time": iso(NOW)},     # not a phone
], own_numbers=[OURS])
ck("one patient paired", n == 1, n)
hits = ix.lines_for("(561) 388-9422")
ck("lookup by any phone format", len(hits) == 1 and hits[0]["ext"] == "111" and hits[0]["name"] == "Ariel Voskin", hits)
ck("first and last span the texts", hits and hits[0]["first"] < hits[0]["last"], hits)
ck("since filter drops old pairs", ix.lines_for(PAT, NOW + timedelta(hours=1)) == [])
ck("since filter keeps recent pairs", len(ix.lines_for(PAT, NOW - timedelta(days=2))) == 1)
ix.record("111", [{"direction": "Inbound", "from_number": PAT, "start_time": iso(NOW - timedelta(days=9))}], [OURS])
ck("an older text widens first", ix.lines_for(PAT)[0]["first"] == SI._iso(NOW - timedelta(days=9)), ix.lines_for(PAT))
ix.mark("111", synced_to=NOW, filled_from=NOW - timedelta(days=5))
ix.set_lines({"111": {"name": "Ariel V.", "ext_no": "689", "numbers": [OURS]}})
l1 = ix.line("111")
ck("directory refresh keeps progress", l1["synced_to"] and l1["filled_from"] and l1["name"] == "Ariel V.", l1)
ck("a line that left the directory stays", ix.line("222") is not None)
ck("no text is stored", all("text" not in c.lower() and "message" not in c.lower()
                            for c in [r[1] for r in ix._db().execute("pragma table_info(pairs)")]))

# ---- one loop pass and the search, through app.py --------------------------
os.environ.setdefault("SMS_INDEX", "1")
import app as A  # noqa: E402

reads = []


class FakeRC:
    SMS_RATE_GROUP = "sms"
    configured = True
    server_url = "https://x"
    account_id = "~"

    def rate_limited(self, g):
        return False

    def cooldown_remaining(self, g):
        return 0

    def _rc_headers(self):
        return {"Authorization": "Bearer T"}

    def fetch_extension_messages(self, ext, start, end, max_pages=6, max_wait=0.0, timeout=15.0,
                                 keep_text=False, phone_number=""):
        reads.append({"ext": ext, "start": start, "end": end, "max_pages": max_pages,
                      "keep_text": keep_text, "phone": phone_number})
        if ext == "busy" and not phone_number:
            # a blast line: more than one read holds; newest first
            rows = [{"direction": "Outbound", "from_number": OURS, "to_number": "+1212555%04d" % i,
                     "start_time": iso(end - timedelta(minutes=i))} for i in range(50)]
            return rows, {"pages": max_pages, "truncated": True, "note": "Hit the page cap"}
        if ext == "broken":
            return [], {"pages": 1, "http_error": 500, "note": "HTTP 500"}
        rows = [{"direction": "Inbound", "from_number": PAT, "to_number": OURS,
                 "start_time": iso(end - timedelta(hours=1)), "text": "I would like to reschedule" if keep_text else None}]
        return rows, {"pages": 1}


saved = (A._smsidx, A._ringcx, A._sms_index_directory, A.SMS_INDEX_GAP, A.SMS_INDEX_FILL_PER_PASS, A.SMS_INDEX_DAYS)
try:
    A._smsidx = SI.SmsIndex(os.path.join(tmp, "b.sqlite"))
    A._ringcx = FakeRC()
    A._sms_index_directory = lambda: {"quiet": {"name": "Judith Merlo", "ext_no": "185", "numbers": [OURS]},
                                      "busy": {"name": "Sally", "ext_no": "151", "numbers": ["+19294153463"]},
                                      "broken": {"name": "Gone", "ext_no": "999", "numbers": []}}
    A.SMS_INDEX_GAP, A.SMS_INDEX_FILL_PER_PASS, A.SMS_INDEX_DAYS = 0, 10, 30
    A._sms_index_pass()
    q, b, br = A._smsidx.line("quiet"), A._smsidx.line("busy"), A._smsidx.line("broken")
    ck("quiet line synced", q["synced_to"] is not None and q["last_error"] is None, q)
    # each read of the blast line holds 50 minutes; the fill walks back by what it read, never a whole window
    ck("busy line: filled back only as far as rows were read",
       b["filled_from"] and SI.parse_iso(b["synced_to"]) - timedelta(hours=6) < SI.parse_iso(b["filled_from"])
       < SI.parse_iso(b["synced_to"]) - timedelta(minutes=45), b)
    ck("broken line: error kept, not synced", br["synced_to"] is None and br["last_error"], br)
    pages_of = {"busy": 8}
    fill = [r for r in reads if r["max_pages"] == 8]
    fill_pages = sum(pages_of.get(r["ext"], 1) for r in fill)
    ck("the fill stops at its page budget (one read of overshoot at most)",
       0 < fill_pages <= A.SMS_INDEX_FILL_PER_PASS + 8 and fill_pages - pages_of.get(fill[-1]["ext"], 1) < A.SMS_INDEX_FILL_PER_PASS,
       (fill_pages, [r["ext"] for r in fill]))
    ck("the index never asks for text", all(not r["keep_text"] for r in reads), reads[:3])
    ck("quiet line filled back toward 30 days",
       SI.parse_iso(A._smsidx.line("quiet")["filled_from"]) < NOW - timedelta(days=2), A._smsidx.line("quiet"))

    # the search: only indexed lines, phone-filtered, with text
    reads.clear()
    A.GOOGLE_CLIENT_ID = ""
    A._ringcx.search_call_history = lambda p, days=30: []
    A._ringcx.search_ringcx_call_history = lambda p, days=30: []
    A._zoho._fetch_all_calls_for_phones = lambda ps: {}
    A._zoho.search_sms_history = lambda p: []
    c = A.app.test_client()
    j = c.get("/api/call-history/search?q=5613889422&days=30").get_json()
    rs = j.get("rc_sms") or []
    ck("search returns the RingCentral text", len(rs) == 1 and rs[0]["message"] == "I would like to reschedule", j.get("rc_sms"))
    ck("signed by the line's owner", rs and rs[0]["line"] == "Judith Merlo", rs)
    ck("read live with text, phone-filtered as +1", reads and all(r["keep_text"] and r["phone"] == "+15613889422" for r in reads), reads)
    ck("only the line that texted this number is read", {r["ext"] for r in reads} == {"quiet"}, reads)
    ck("coverage rides along", (j.get("rc_sms_meta") or {}).get("index", {}).get("lines") == 3, j.get("rc_sms_meta"))
    reads.clear()
    j = c.get("/api/call-history/search?q=3105550000&days=30").get_json()
    ck("a number no line texted reads nothing", not reads and j.get("rc_sms") == [], (reads, j.get("rc_sms")))
finally:
    (A._smsidx, A._ringcx, A._sms_index_directory, A.SMS_INDEX_GAP, A.SMS_INDEX_FILL_PER_PASS, A.SMS_INDEX_DAYS) = saved

print("%d passed, %d failed" % (passed, len(errors)))
for e in errors:
    print("  FAIL", e)
sys.exit(1 if errors else 0)
