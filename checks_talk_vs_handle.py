"""The board reports TALK time; the RingCX report's column is HANDLE time.

Comparing the two reads as a discrepancy when it is two different
measurements. On 2026-08-26 the Scheduling board showed Sarahi Rivera at 95.5m
against 121.6m in the RingCX export -- and the call counts matched exactly
(27/70/71/64 on both), which is what said the pipeline was reading the same
rows and only the metric differed.

    Handle Time = Talk Time + Wrap Time

verified against the real export, exactly, per agent. The implied wrap was 4 to
24 seconds a call, which is ordinary after-call work.

So the board now carries wrap and the handle total it adds up to, and says
which time it is showing. This pins the arithmetic so the two reports stay
reconcilable.

Run with no arguments.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from billing_report import build_report  # noqa: E402


def _row(talk, wrap, direction="Inbound", result="Accepted"):
    return {"direction": direction, "result": result, "duration": talk,
            "wrap_seconds": wrap, "source": "ringcx",
            "start_time": "2026-08-26T14:00:00+00:00"}


def run():
    fails = 0
    rep = build_report(
        {"A": [_row(600, 60), _row(300, 30), _row(120, 0)],
         # A ringing call that never connected contributes NEITHER talk nor wrap.
         "B": [_row(0, 45, "Outbound", "No Answer"), _row(240, 15, "Outbound",
                                                          "Call connected")]},
        tz_offset_minutes=-240, window={"start": "2026-08-26", "end": "2026-08-26"})
    by = {a["name"]: a for a in rep.get("ranked", []) + rep.get("stalled", [])
          + rep.get("silent", [])}

    a, b = by.get("A", {}), by.get("B", {})
    cases = [
        ("A talk is talk only", a.get("per_day", {}).get("talk_minutes"), 17.0),
        ("A wrap is summed", a.get("wrap_minutes"), 1.5),
        ("A handle is talk + wrap", a.get("handle_minutes"), 18.5),
        # The identity itself, which is what makes the two reports comparable.
        ("identity holds for A",
         round((a.get("per_day", {}).get("talk_minutes") or 0)
               + (a.get("wrap_minutes") or 0), 1), a.get("handle_minutes")),
        # An unconnected call is not work time on either measure.
        ("unconnected wrap is excluded", b.get("wrap_minutes"), 0.2),
        ("...and its talk too", b.get("per_day", {}).get("talk_minutes"), 4.0),
    ]

    # A missing or junk wrap value must not break the row or invent time.
    rep2 = build_report({"C": [{"direction": "Inbound", "result": "Accepted",
                                "duration": 300, "source": "ringcx",
                                "start_time": "2026-08-26T14:00:00+00:00"},
                               {"direction": "Inbound", "result": "Accepted",
                                "duration": 300, "wrap_seconds": "junk",
                                "source": "ringcx",
                                "start_time": "2026-08-26T14:00:00+00:00"}]},
                        tz_offset_minutes=-240,
                        window={"start": "2026-08-26", "end": "2026-08-26"})
    c = next((x for x in rep2.get("ranked", []) + rep2.get("silent", [])
              + rep2.get("stalled", []) if x["name"] == "C"), {})
    cases += [
        ("absent wrap is zero, not None", c.get("wrap_minutes"), 0.0),
        ("junk wrap invents nothing", c.get("handle_minutes"), 10.0),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-32s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
