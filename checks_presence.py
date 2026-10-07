"""On-call status, and the three ways it could lie.

Danny, 2026-10-05: "add on call status to the billing tracker ... as on call, not
on call etc."

A presence board is mostly a problem of absence. Four different things arrive as
"this seat is not shown on a call", and only one of them means the phone is free:
  * the phone is genuinely idle                    -> Available / Offline
  * the seat was not in the presence response      -> NOT READ
  * the presence call stood down on a rate cooldown-> NOT READ
  * the last read is minutes old                   -> WITHHELD, not shown as now
Printing "Not on a call" for any of the last three invents the most reassuring
reading of a missing fact, on a board a floor lead acts on.

It also must not be able to break the thing it decorates. Presence with
detailedTelephonyState is in RingCentral's "heavy" class -- the same ~10/minute
budget the call log spends, which is ALREADY short on this account -- so the
report path must never touch it.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
import time

# GOOGLE_CLIENT_ID deliberately unset: _v6_allowed() opens the endpoint only when
# no Google client is configured. The gate has its own checks.
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

A.app.config["TESTING"] = True
CLIENT = A.app.test_client()
ROSTER = [s["name"] for s in A._billing_roster("billing")[0]]
SEAT = {s["name"]: s for s in A._billing_roster("billing")[0]}


def rec(seat_name, tel="NoCall", status="Available", dnd="", ext_number=None):
    s = SEAT[seat_name]
    return {"ext_id": str(s["ext_id"]),
            "ext_number": str(ext_number if ext_number is not None else s["ext"]),
            "name": seat_name, "status": status, "dnd_status": dnd,
            "telephony_status": tel, "active_calls": []}


def probe(agents, age=5.0, note=None, never=False, stale=False):
    A._ringcx.agent_statuses_with_age = lambda: (
        agents, {"age_seconds": age, "stale": stale, "never_read": never,
                 "note": note, "ttl_seconds": 60})
    r = CLIENT.get("/api/v6/presence")
    return r.get_json() if r.status_code == 200 else {"_http": r.status_code}


_real = A._ringcx.agent_statuses_with_age
try:
    # ---- the states ----
    j = probe([rec("Vivian Martinez", tel="CallConnected"),
               rec("Yareth Pavon", tel="Ringing"),
               rec("Gabriela Maldonado", tel="OnHold"),
               rec("Ana Salazar", tel="NoCall", status="Available")])
    by = {s["name"]: s for s in j.get("seats", [])}
    ck("every roster seat is reported", sorted(by) == sorted(ROSTER), sorted(by))
    ck("a connected call is ON CALL",
       by.get("Vivian Martinez", {}).get("on_call") is True, by.get("Vivian Martinez"))
    ck("and is labelled for a human", by.get("Vivian Martinez", {}).get("label") == "On a call",
       by.get("Vivian Martinez"))
    # A ringing phone is not a conversation. Folding it into "on a call" would
    # overstate how busy the floor is at exactly the moment someone checks.
    ck("ringing is NOT on a call", by.get("Yareth Pavon", {}).get("on_call") is False,
       by.get("Yareth Pavon"))
    ck("but ringing is still named", by.get("Yareth Pavon", {}).get("label") == "Ringing",
       by.get("Yareth Pavon"))
    ck("on hold is NOT on a call", by.get("Gabriela Maldonado", {}).get("on_call") is False,
       by.get("Gabriela Maldonado"))
    ck("on hold is named", by.get("Gabriela Maldonado", {}).get("label") == "On hold",
       by.get("Gabriela Maldonado"))
    ck("an idle phone is Available", by.get("Ana Salazar", {}).get("state") == "available",
       by.get("Ana Salazar"))

    # ---- absence is not idleness ----
    j = probe([rec("Vivian Martinez", tel="CallConnected")])
    by = {s["name"]: s for s in j.get("seats", [])}
    missing = by.get("Ana Salazar", {})
    ck("a seat absent from presence is NOT READ", missing.get("state") == "unknown", missing)
    ck("and is not labelled as free", missing.get("label") == "Not read", missing)
    ck("and is not claimed to be on a call", missing.get("on_call") is False, missing)
    ck("and says it was not matched", missing.get("matched") is False, missing)
    ck("the seat that WAS reported still reads",
       by.get("Vivian Martinez", {}).get("on_call") is True, by.get("Vivian Martinez"))

    # ---- a cooldown is not idleness ----
    j = probe([], age=None, never=True, note="RingEX is in a shared cooldown")
    ck("nothing read -> every seat unknown",
       all(s["state"] == "unknown" for s in j.get("seats", [])), j.get("seats"))
    ck("nothing read -> withheld", j.get("withheld") is True, j.get("withheld"))
    ck("and the reason is carried", bool(j.get("note")), j.get("note"))

    # ---- stale is not now ----
    j = probe([rec("Vivian Martinez", tel="CallConnected")], age=600.0, stale=True)
    by = {s["name"]: s for s in j.get("seats", [])}
    ck("a 10-minute-old 'on a call' is NOT shown as on a call",
       by.get("Vivian Martinez", {}).get("on_call") is False, by.get("Vivian Martinez"))
    ck("stale is withheld, not relabelled idle",
       by.get("Vivian Martinez", {}).get("state") == "unknown", by.get("Vivian Martinez"))
    ck("the age is still reported", j.get("as_of_age_seconds") == 600.0,
       j.get("as_of_age_seconds"))
    ck("and withheld says so", j.get("withheld") is True, j.get("withheld"))
    # Fresh enough must still work, or the whole feature is withheld forever.
    j = probe([rec("Vivian Martinez", tel="CallConnected")], age=65.0, stale=True)
    by = {s["name"]: s for s in j.get("seats", [])}
    ck("just past its TTL is still shown (stale != useless)",
       by.get("Vivian Martinez", {}).get("on_call") is True, by.get("Vivian Martinez"))

    # ---- do-not-disturb outranks a cheerful presenceStatus ----
    j = probe([rec("Vivian Martinez", tel="NoCall", status="Available",
                   dnd="DoNotAcceptAnyCalls")])
    by = {s["name"]: s for s in j.get("seats", [])}
    ck("DND is not reported as Available", by.get("Vivian Martinez", {}).get("state") == "dnd",
       by.get("Vivian Martinez"))

    # ---- presence answers the ext_id question for free ----
    j = probe([rec("Ana Salazar", tel="NoCall", ext_number="999")])
    by = {s["name"]: s for s in j.get("seats", [])}
    ana = by.get("Ana Salazar", {})
    ck("an ext_id that is a DIFFERENT extension is flagged", bool(ana.get("ext_mismatch")), ana)
    ck("the mismatch names both numbers",
       "271" in (ana.get("ext_mismatch") or "") and "999" in (ana.get("ext_mismatch") or ""),
       ana.get("ext_mismatch"))
    j = probe([rec("Ana Salazar", tel="NoCall")])
    ck("a matching extension raises nothing",
       not {s["name"]: s for s in j["seats"]}["Ana Salazar"].get("ext_mismatch"),
       j["seats"])
finally:
    A._ringcx.agent_statuses_with_age = _real

# ---- RingCX seats: call status from the active-calls list ----
# Danny, 2026-10-07: "add their call statuses here for RingCX agents using the
# same connection or data source". Three facts have to stay apart: on a call
# (in the list), no active call (absent from a list that WAS read -- the list
# is complete, so that is a real negative), and not read (the fetch failed).
def cxprobe(calls, ok=True, note=None, team="surgical"):
    A._cx_active_cache.update(at=0.0, calls=[], meta=None)
    A._ringcx.active_calls_with_status = lambda: (calls, {"ok": ok, "note": note,
                                                          "read_at": time.time(), "http_error": None})
    r = CLIENT.get("/api/v6/presence?team=%s" % team)
    return r.get_json() if r.status_code == 200 else {"_http": r.status_code}

_real_cx = A._ringcx.active_calls_with_status
try:
    j = cxprobe([{"agent_name": "Judith  Merlo", "call_state": "ACTIVE", "direction": "INBOUND",
                  "queue_name": "Scheduling", "duration_sec": 125},
                 {"agent_name": "Oscar Caballero", "call_state": "ON_HOLD", "duration_sec": 30}])
    by = {x["name"]: x for x in j.get("seats", [])}
    ck("a RingCX seat on an active call is ON CALL", by.get("Judith Merlo", {}).get("on_call") is True,
       by.get("Judith Merlo"))
    ck("matched despite doubled spaces in the report's name", by.get("Judith Merlo", {}).get("matched") is True,
       by.get("Judith Merlo"))
    ck("the call's queue and length ride along", (by.get("Judith Merlo", {}).get("call") or {}).get("queue") == "Scheduling",
       by.get("Judith Merlo", {}).get("call"))
    ck("HOLD is on hold, not on a call", by.get("Oscar Caballero", {}).get("state") == "on_hold", by.get("Oscar Caballero"))
    ck("a seat absent from a READ list has no active call (a real negative)",
       by.get("Johana Duron", {}).get("state") == "idle" and by.get("Johana Duron", {}).get("label") == "No active call",
       by.get("Johana Duron"))
    ck("and is not on a call", by.get("Johana Duron", {}).get("on_call") is False, by.get("Johana Duron"))
    ck("the surgical table is no longer withheld", j.get("withheld") is False, j.get("withheld"))
    ck("every surgical seat is reported", len(by) == len(A._billing_roster("surgical")[0]), sorted(by))

    j = cxprobe([], ok=False, note="RingCX returned HTTP 401 reading active calls")
    by = {x["name"]: x for x in j.get("seats", [])}
    ck("a FAILED read is NOT 'no active call'", by.get("Judith Merlo", {}).get("state") == "unknown", by.get("Judith Merlo"))
    ck("and says why", "401" in (by.get("Judith Merlo", {}).get("note") or ""), by.get("Judith Merlo"))
    ck("the board is withheld when nothing could be read", j.get("withheld") is True, j.get("withheld"))

    # one RingCX read serves many boards
    calls_made = []
    A._cx_active_cache.update(at=0.0, calls=[], meta=None)
    A._ringcx.active_calls_with_status = lambda: (calls_made.append(1), ([], {"ok": True, "note": None, "read_at": time.time(), "http_error": None}))[1]
    CLIENT.get("/api/v6/presence?team=surgical"); CLIENT.get("/api/v6/presence?team=sales"); CLIENT.get("/api/v6/presence?team=scheduling")
    ck("three boards within 30s cost ONE RingCX read", len(calls_made) == 1, len(calls_made))
finally:
    A._ringcx.active_calls_with_status = _real_cx
    A._cx_active_cache.update(at=0.0, calls=[], meta=None)

# ---- presence must not be able to take the board down ----
called = []
_saved = (A._ringcx.agent_statuses_with_age, A._ringcx.get_agent_statuses,
          A._v6_fetch_ringex, A._v6_fetch_sms, A._v6_finish)
A._ringcx.agent_statuses_with_age = lambda: (called.append("age"), ([], {}))[1]
A._ringcx.get_agent_statuses = lambda: (called.append("raw"), [])[1]
A._v6_fetch_ringex = lambda seats, days, lt, tz: (
    {x["name"]: {"rows": [], "ext": x["ext"], "ext_id": x["ext_id"],
                 "complete": True, "missing_days": []} for x in seats},
    {"cached": 0, "fetched": 0, "missing": 0})
A._v6_fetch_sms = lambda r_, d_, lt, tz: ({}, {})
A._v6_finish = lambda *a, **k: {"ok": True}
try:
    A._v6_build("2026-10-05", "2026-10-05", 240, "2026-10-05", team="billing")
finally:
    (A._ringcx.agent_statuses_with_age, A._ringcx.get_agent_statuses,
     A._v6_fetch_ringex, A._v6_fetch_sms, A._v6_finish) = _saved
ck("building the report never spends presence budget", called == [], called)

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
