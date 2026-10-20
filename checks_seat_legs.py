"""A call answered on one device while RingCentral sums it up from another.

Danny, 2026-10-20, forwarding Yareth Pavon: "This was the call, pt was
transferred to me from sales". Gabriel Johnson transferred a patient to her;
she talked 1,394 s on the app, and her call log recorded it as an OUTBOUND call
from the patient's number to her own (offline) desk phone, "IP Phone Offline",
0 s -- the board counted a failed dial and no talk. The record shapes below are
hers, with the numbers replaced.
Run: ./.venv/bin/python -B checks_seat_legs.py   (tally LAST)
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import billing_report as br  # noqa: E402
import ringcx_client as RC  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


EXT = 998743035
PT = "+14195550101"
TRANSFER_IN = {
    "id": "t1", "sessionId": "s1", "startTime": "2026-10-20T17:07:36.469Z", "direction": "Outbound",
    "result": "IP Phone Offline", "duration": 0, "action": "VoIP Call", "reason": "No Digital Line",
    "from": {"name": "Yareth Pavon", "phoneNumber": PT, "extensionId": str(EXT)},
    "to": {"phoneNumber": "+16465550259", "extensionNumber": "220"},
    "legs": [{"legType": "PstnToSip", "direction": "Outbound", "result": "Call connected", "duration": 1394},
             {"legType": "PstnToSip", "direction": "Outbound", "result": "IP Phone Offline", "duration": 0, "master": True}]}
ANSWERED = {
    "id": "a1", "startTime": "2026-10-20T15:12:00.000Z", "direction": "Inbound", "result": "Accepted", "duration": 77,
    "from": {"phoneNumber": "+14325550102"}, "to": {"name": "Yareth Pavon", "phoneNumber": "+19545551121"},
    "legs": [{"legType": "Accept", "result": "Accepted", "duration": 77},
             {"legType": "PstnToSip", "result": "Call connected", "duration": 77},
             {"legType": "PstnToSip", "result": "IP Phone Offline", "duration": 0}]}
MISSED = {
    "id": "m1", "startTime": "2026-10-20T14:03:13.000Z", "direction": "Inbound", "result": "Missed", "duration": 12,
    "from": {"phoneNumber": "+13145550103"}, "to": {"name": "Yareth Pavon", "phoneNumber": "+19545551121"},
    "legs": [{"legType": "Accept", "result": "Missed", "duration": 12},
             {"legType": "PstnToSip", "result": "IP Phone Offline", "duration": 0}]}
DIAL = {
    "id": "d1", "startTime": "2026-10-20T14:46:45.000Z", "direction": "Outbound", "result": "Call connected", "duration": 36,
    "from": {"name": "Yareth Pavon", "phoneNumber": "+19545551121", "extensionId": str(EXT)}, "to": {"phoneNumber": "+14045550104"},
    "legs": [{"legType": "SipToPstnUnmetered", "result": "Call connected", "duration": 36}]}
HIDDEN_DIAL = {  # an outbound whose summary came from a dead leg, to an outside number
    "id": "h1", "startTime": "2026-10-20T16:00:00.000Z", "direction": "Outbound", "result": "IP Phone Offline", "duration": 0,
    "from": {"name": "Yareth Pavon", "phoneNumber": "+19545551121", "extensionId": str(EXT)}, "to": {"phoneNumber": "+14045550105"},
    "legs": [{"result": "Call connected", "duration": 300}, {"result": "IP Phone Offline", "duration": 0, "master": True}]}
SHORT_NOISE = {  # a leg "connected" for a shorter time than the summary already says: leave it
    "id": "n1", "startTime": "2026-10-20T16:10:00.000Z", "direction": "Outbound", "result": "No Answer", "duration": 40,
    "from": {"name": "Yareth Pavon", "phoneNumber": "+19545551121", "extensionId": str(EXT)}, "to": {"phoneNumber": "+14045550106"},
    "legs": [{"result": "Call connected", "duration": 5}]}

t = RC._seat_row(TRANSFER_IN, EXT)
ck("a transfer in is an inbound call from the patient, answered, with the app leg's seconds",
   t["direction"] == "Inbound" and t["result"] == "Accepted" and t["duration"] == 1394 and t["from_number"] == PT
   and t["fixed"] == "transfer_in", t)
a = RC._seat_row(ANSWERED, EXT)
ck("an answered call stays exactly as RingCentral summed it up", a["direction"] == "Inbound" and a["result"] == "Accepted"
   and a["duration"] == 77 and "fixed" not in a, a)
m = RC._seat_row(MISSED, EXT)
ck("a missed call that rang only the offline desk phone stays missed", m["result"] == "Missed" and "fixed" not in m, m)
d = RC._seat_row(DIAL, EXT)
ck("an ordinary dial is untouched", d == dict(d, direction="Outbound", result="Call connected", duration=36) and "fixed" not in d, d)
h = RC._seat_row(HIDDEN_DIAL, EXT)
ck("an outbound summed up from a dead leg takes the connected leg: still a dial, connected, 300 s",
   h["direction"] == "Outbound" and h["result"] == "Call connected" and h["duration"] == 300 and h["fixed"] == "connected_leg", h)
n = RC._seat_row(SHORT_NOISE, EXT)
ck("a leg shorter than the summary changes nothing", n["result"] == "No Answer" and n["duration"] == 40 and "fixed" not in n, n)
other = RC._seat_row(TRANSFER_IN, 111)
ck("the transfer-in reading needs the seat's OWN extension on the record", other["direction"] == "Outbound"
   and other["result"] == "Call connected" and other["fixed"] == "connected_leg", other)
ck("a record with no legs (the old Simple view) is read as before", RC._seat_row(dict(TRANSFER_IN, legs=[]), EXT)["result"] == "IP Phone Offline", "simple")

# ---- on the board: talk, dials, answered, >10 min ----
def seat_day(recs):
    rows = [RC._seat_row(r, EXT) for r in recs]
    rep = br.build_report({"Yareth Pavon": {"rows": rows, "ext": "220", "ext_id": EXT, "complete": True}},
                          tz_offset_minutes=-240, window={"start": "2026-10-20", "end": "2026-10-20"})
    s = next(x for v in rep.values() if isinstance(v, list) for x in v if isinstance(x, dict) and x.get("name") == "Yareth Pavon")
    return s["days"][-1], s


before, _ = seat_day([dict(r, legs=[]) for r in (TRANSFER_IN, ANSWERED, MISSED, DIAL)])
after, s_after = seat_day([TRANSFER_IN, ANSWERED, MISSED, DIAL])
ck("the transfer was a 0-second failed dial before (the bug, reproduced)",
   before["talk_seconds"] == 77 + 36 and before["dials"] == 2 and before["inbound_answered"] == 1, before)
ck("...and is 23 minutes of talk, an answered inbound and a call over 10 minutes now, not a dial",
   after["talk_seconds"] == 77 + 36 + 1394 and after["dials"] == 1 and after["inbound_answered"] == 2
   and after["over_600s"] == 1, after)
ck("the patient's number is the transfer's number, so a miss from them before it reads answered",
   RC._seat_row(TRANSFER_IN, EXT)["from_number"] == PT, "number")

# ---- the reader asks for the legs ----
asked = []


class _R:
    status_code, ok = 200, True

    def json(self):
        return {"records": [TRANSFER_IN, DIAL]}


cli = RC.RingCXClient.__new__(RC.RingCXClient)
cli.server_url, cli.account_id = "https://example.invalid", "~"
cli._cool_until, cli._cool_until_groups, cli._cool_reason = 0.0, {}, None
cli._ensure_rc_token = lambda: "t"
cli._rc_headers = lambda: {}
real_get = RC.requests.get
RC.requests.get = lambda url, **k: (asked.append(k.get("params")), _R())[1]
try:
    from datetime import datetime, timezone
    rows, meta = cli.fetch_extension_calls(EXT, datetime(2026, 10, 20, 4, tzinfo=timezone.utc), datetime(2026, 10, 21, 4, tzinfo=timezone.utc))
finally:
    RC.requests.get = real_get
ck("the seat reader asks for the Detailed view (the legs), once for a short day",
   len(asked) == 1 and asked[0]["view"] == "Detailed", asked)
ck("...and its rows come through the same correction", any(r.get("fixed") == "transfer_in" and r["duration"] == 1394 for r in rows), rows)

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
