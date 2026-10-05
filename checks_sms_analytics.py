"""SMS analytics on the KPI boards.

The two claims worth defending here are the ones that would quietly mislead a
manager rather than crash:

  1. A message store that could not be READ must never render as "sent 0".
     A refused fetch and a silent phone are the same empty list, and this board
     names individuals -- the same defect that once printed "logged no calls at
     all" under three people who had simply been rate-limited.

  2. SMS must not be able to take the CALL board down. RingCentral meters
     message-store separately from call-log, so a 429 on one says nothing about
     the other; one shared cooldown would let the texting reader pause the
     figures this service exists to serve.
"""
import sys

from billing_report import build_report, _fold_sms, _blank_sms
from ringcx_client import RingCXClient


def _call(day, direction="Outbound", result="Call connected", dur=120):
    return {"direction": direction, "result": result, "duration": dur,
            "start_time": f"{day}T15:00:00.000Z"}


def _sms(day, direction="Outbound"):
    return {"direction": direction, "type": "SMS",
            "start_time": f"{day}T15:00:00.000Z"}


def _eq(label, got, want, fails):
    ok = got == want
    print("  %-52s %s (got %r)" % (label, "ok" if ok else "MISMATCH", got))
    return fails + (0 if ok else 1)


def case_direction_mapping():
    """Outbound is sent, Inbound is received. Never summed into one figure."""
    f = 0
    b = _blank_sms()
    for r in [_sms("2026-10-01"), _sms("2026-10-01"),
              _sms("2026-10-01", "Inbound")]:
        _fold_sms(b, r)
    f = _eq("outbound counted as sent", b["sent"], 2, f)
    f = _eq("inbound counted as received", b["received"], 1, f)
    f = _eq("total is both", b["total"], 3, f)
    return f


def case_unread_is_not_zero():
    """THE one. An incomplete read is `unknown`, not a zero score."""
    f = 0
    calls = {"Vivian": {"rows": [_call("2026-10-01")], "ext": "137",
                        "ext_id": "1", "complete": True, "missing_days": []}}
    # Message store refused: no rows AND the window was not covered.
    sms = {"Vivian": {"rows": [], "complete": False,
                      "missing_days": ["2026-10-01"]}}
    rep = build_report(calls, window={"start": "2026-10-01", "end": "2026-10-01"},
                       sms_by_agent=sms)
    a = (rep["ranked"] + rep["silent"] + rep["stalled"] + rep["unknown"])[0]
    f = _eq("unread window flagged unknown", a["sms"]["unknown"], True, f)
    f = _eq("unread window not marked complete", a["sms"]["complete"], False, f)
    f = _eq("a warning names the partial seat",
            any(w["kind"] == "sms_incomplete" for w in rep["warnings"]), True, f)

    # The negative half: a seat that genuinely sent nothing, fully read, is NOT
    # unknown. Without this the check passes by calling everything unknown.
    sms_ok = {"Vivian": {"rows": [], "complete": True, "missing_days": []}}
    rep2 = build_report(calls, window={"start": "2026-10-01", "end": "2026-10-01"},
                        sms_by_agent=sms_ok)
    a2 = (rep2["ranked"] + rep2["silent"] + rep2["stalled"] + rep2["unknown"])[0]
    f = _eq("genuine zero is NOT unknown", a2["sms"]["unknown"], False, f)
    f = _eq("genuine zero reports 0 sent", a2["sms"]["sent"], 0, f)
    f = _eq("no warning for a complete read",
            any(w["kind"] == "sms_incomplete" for w in rep2["warnings"]), False, f)
    return f


def case_sms_does_not_touch_call_figures():
    """Texting is not calling: SMS must not create or alter a working day."""
    f = 0
    calls = {"Vivian": {"rows": [_call("2026-10-01")], "ext": "137", "ext_id": "1",
                        "complete": True, "missing_days": []}}
    base = build_report(calls, window={"start": "2026-10-01", "end": "2026-10-02"})
    # 50 messages on a day with NO connected call must not invent a working day.
    sms = {"Vivian": {"rows": [_sms("2026-10-02") for _ in range(50)],
                      "complete": True, "missing_days": []}}
    withs = build_report(calls, window={"start": "2026-10-01", "end": "2026-10-02"},
                         sms_by_agent=sms)
    a0 = (base["ranked"] + base["silent"])[0]
    a1 = (withs["ranked"] + withs["silent"])[0]
    f = _eq("worked_days unchanged by SMS", a1["worked_days"], a0["worked_days"], f)
    f = _eq("talk/day unchanged by SMS",
            a1["per_day"]["talk_minutes"], a0["per_day"]["talk_minutes"], f)
    f = _eq("calls/day unchanged by SMS",
            a1["per_day"]["calls"], a0["per_day"]["calls"], f)
    f = _eq("the 50 messages ARE still counted", a1["sms"]["sent"], 50, f)
    return f


def case_reader_off_leaves_board_identical():
    """sms_by_agent=None is the switch-off path: no sms key, no zeros."""
    f = 0
    calls = {"Vivian": {"rows": [_call("2026-10-01")], "ext": "137", "ext_id": "1",
                        "complete": True, "missing_days": []}}
    rep = build_report(calls, window={"start": "2026-10-01", "end": "2026-10-01"})
    a = (rep["ranked"] + rep["silent"])[0]
    f = _eq("sms is None when never read", a["sms"], None, f)
    f = _eq("no team sms block", "sms" in rep["team"], False, f)
    return f


def case_sms_cooldown_does_not_pause_calls():
    """A 429 on the message store must not stop the call board."""
    f = 0
    c = RingCXClient()
    c.note_rate_limited(60, group=c.SMS_RATE_GROUP)
    f = _eq("sms group IS paused", c.rate_limited(c.SMS_RATE_GROUP), True, f)
    f = _eq("call path is NOT paused", c.rate_limited(), False, f)

    # And the other direction: the call path's own cooldown still works, so the
    # scoping did not simply disable it.
    c2 = RingCXClient()
    c2.note_rate_limited(60)
    f = _eq("call 429 still pauses calls", c2.rate_limited(), True, f)
    f = _eq("call 429 does not pause sms",
            c2.rate_limited(c2.SMS_RATE_GROUP), False, f)
    return f


def run():
    total = 0
    for title, fn in [
        ("sent/received direction mapping", case_direction_mapping),
        ("an UNREAD message store is not a zero", case_unread_is_not_zero),
        ("SMS does not alter call figures", case_sms_does_not_touch_call_figures),
        ("reader off leaves the board identical", case_reader_off_leaves_board_identical),
        ("an SMS 429 does not pause the call board", case_sms_cooldown_does_not_pause_calls),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
