"""A seat may be read from a platform its team does not use, and a seat taken off
the board must not reappear below it.

Danny, 2026-10-05, in two steps:
  "pull in Ana's CX data and list it at 0.0 if its not pulled but put her on the
   chart, and take Andrea off from there."
  then: "Ana is supposed to be extension 271 on the RingEX side" -> "flip her
   back to RingEX".

So Ana is a RingEX billing seat that is RANKED EVEN AT ZERO. The per-seat source
override stays, because it is what makes that a one-line change instead of a
rewrite -- but it is exercised here with a SYNTHETIC seat. Pinning a mechanism's
test to a real person is what broke eleven assertions in checks_collector_calls
when she was promoted, and it would have broken these when she was flipped back.

Four things can go wrong, and each has a case:
  1. _billing_roster REBUILDS every seat from three keys, so always_rank/source
     are easy to drop -- silently reverting a seat to its team's platform.
  2. A zero-call seat is normally held OUT of the ranking by design. always_rank
     must override that, and ONLY for the seat that asked for it.
  3. A ranked 0.0 must still say WHY it is zero, and must distinguish "read in
     full, no calls" from "could not be read".
  4. Dropping a seat from the roster is not taking it off the board: its sheet tab
     still maps to the name, so it returns as an untracked collector.
"""
import sys

errors, passed = [], 0


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


import app as A  # noqa: E402
from billing_report import build_report  # noqa: E402

# ---- 1. the roster: who is on it, and are the optional keys carried ----
roster, meta = A._billing_roster("billing")
by = {s["name"]: s for s in roster}
ck("Andrea Pleasant is off the billing roster", "Andrea Pleasant" not in by, sorted(by))
ck("Ana Salazar is on the billing roster", "Ana Salazar" in by, sorted(by))
ana = by.get("Ana Salazar", {})
ck("Ana is on extension 271", ana.get("ext") == "271", ana)
# Flipped back to RingEX on 2026-10-05: her calls come from the board's own
# platform, so she must carry NO source override.
ck("Ana is read from RingEX, not RingCX", "source" not in ana, ana)
ck("Ana is ranked even at zero", ana.get("always_rank") is True, ana)
ck("a plain seat keeps the three-key shape",
   set(by.get("Vivian Martinez", {})) == {"name", "ext_id", "ext"},
   by.get("Vivian Martinez"))

# The optional keys must survive the rebuild. Asserted on a synthetic seat so it
# holds whoever is or is not overridden in the real roster.
_clean = A._billing_roster.__wrapped__ if hasattr(A._billing_roster, "__wrapped__") else None
_real_rosters = A._TEAM_ROSTERS
SYNTH = {"name": "Marisol Vega", "ext_id": 999000111, "ext": "299",
         "source": "ringcx", "source_team": "inbound", "always_rank": True}
A._TEAM_ROSTERS = dict(_real_rosters,
                       billing=list(_real_rosters["billing"]) + [SYNTH])
try:
    r2, _ = A._billing_roster("billing")
    got_synth = next((x for x in r2 if x["name"] == "Marisol Vega"), {})
    ck("source survives the seat rebuild", got_synth.get("source") == "ringcx", got_synth)
    ck("source_team survives the seat rebuild",
       got_synth.get("source_team") == "inbound", got_synth)
    ck("always_rank survives the seat rebuild",
       got_synth.get("always_rank") is True, got_synth)

    # ---- 5. the split: who is asked of WHICH platform ----
    # If an overridden seat is left in the RingEX half it is scored on a handset
    # its team does not use; if a plain seat is pulled into the RingCX half its
    # live RingEX activity vanishes.
    asked_ex, asked_cx = [], []

    def _fake_ex(seats, days, local_today, tz):
        asked_ex.extend(x["name"] for x in seats)
        return ({x["name"]: {"rows": [], "ext": x["ext"], "ext_id": x["ext_id"],
                             "complete": True, "missing_days": []} for x in seats},
                {"cached": 0, "fetched": 0, "missing": 0})

    def _fake_cx_rows(team, days, seats):
        asked_cx.extend(x["name"] for x in seats)
        return ({x["name"]: [] for x in seats}, len(days), {})

    _saved = (A._v6_fetch_ringex, A._v6_cx_rows_for_team, A._v6_fetch_sms, A._v6_finish)
    seen = {}
    A._v6_fetch_ringex = _fake_ex
    A._v6_cx_rows_for_team = _fake_cx_rows
    A._v6_fetch_sms = lambda roster_, days, lt, tz: ({}, {})
    A._v6_finish = lambda rows_by_agent, *a, **k: seen.setdefault("rows", rows_by_agent)
    try:
        A._v6_build("2026-10-01", "2026-10-02", 240, "2026-10-02", team="billing")
    finally:
        (A._v6_fetch_ringex, A._v6_cx_rows_for_team,
         A._v6_fetch_sms, A._v6_finish) = _saved

    ck("the overridden seat is asked of RingCX", asked_cx == ["Marisol Vega"], asked_cx)
    ck("the overridden seat is NOT also asked of RingEX",
       "Marisol Vega" not in asked_ex, asked_ex)
    ck("Ana IS asked of RingEX now", "Ana Salazar" in asked_ex, asked_ex)
    ck("Ana is NOT asked of RingCX", "Ana Salazar" not in asked_cx, asked_cx)
    ck("every billing seat reaches the report", sorted(seen.get("rows", {})) == sorted(
       ["Ana Salazar", "Gabriela Maldonado", "Marisol Vega",
        "Vivian Martinez", "Yareth Pavon"]), sorted(seen.get("rows", {})))
    got = seen.get("rows", {})
    ck("the overridden seat's half is labelled RingCX",
       got.get("Marisol Vega", {}).get("call_source") == "RingCX", got.get("Marisol Vega"))
    ck("a RingEX seat carries no platform label",
       not got.get("Ana Salazar", {}).get("call_source"), got.get("Ana Salazar"))
finally:
    A._TEAM_ROSTERS = _real_rosters

# ---- 2 & 3. always_rank ranks a zero seat, and says why ----
WIN = {"start": "2026-10-01", "end": "2026-10-05"}
rep = build_report({
    "Ana Salazar": {"rows": [], "ext": "271", "ext_id": 1,
                    "complete": True, "always_rank": True},
    "Vivian Martinez": {"rows": [], "ext": "137", "ext_id": 2, "complete": True},
}, tz_offset_minutes=240, window=WIN)
ck("the zero seat that asked for it is RANKED",
   [a["name"] for a in rep["ranked"]] == ["Ana Salazar"], rep["ranked"])
ck("an ordinary zero seat is still held out",
   [a["name"] for a in rep["silent"]] == ["Vivian Martinez"], rep["silent"])
ck("the ranked seat is not ALSO listed as silent",
   "Ana Salazar" not in [a["name"] for a in rep["silent"]], rep["silent"])
ranked_ana = rep["ranked"][0] if rep["ranked"] else {}
ck("her row shows 0.0 talk, not a blank",
   ranked_ana.get("per_day", {}).get("talk_minutes") == 0.0, ranked_ana.get("per_day"))
kinds = [w["kind"] for w in rep["warnings"]]
ck("a ranked zero still raises a finding", "ranked_at_zero" in kinds, kinds)
msg = " ".join(w["message"] for w in rep["warnings"] if w["kind"] == "ranked_at_zero")
ck("and the finding says the window WAS read", "read in full" in msg, msg)

# The other half: not read must not read as a worked zero.
rep2 = build_report({
    "Ana Salazar": {"rows": [], "ext": "271", "ext_id": 1, "complete": False,
                    "missing_days": ["2026-10-02", "2026-10-05"], "always_rank": True},
}, tz_offset_minutes=240, window=WIN)
ck("unread still ranks her", [a["name"] for a in rep2["ranked"]] == ["Ana Salazar"],
   rep2["ranked"])
m2 = " ".join(w["message"] for w in rep2["warnings"] if w["kind"] == "ranked_at_zero")
ck("unread is called NOT READ, not no-calls", "not read" in m2, m2)
ck("and it says how many days", "2 day(s)" in m2, m2)

# ---- 4. off the board means off the footnote too ----
ck("Andrea is excluded from the collectors footnote",
   "Andrea Pleasant" in A.BILLING_BOARD_EXCLUDE, A.BILLING_BOARD_EXCLUDE)

# ---- the coverage witness: her absence vs her report's absence ----
import tempfile, pathlib  # noqa: E402
TMP = pathlib.Path(tempfile.mkdtemp()) / "inbox"
TMP.mkdir(parents=True)
A.RINGCX_INBOX_DIR = TMP
A._inbox_parse_cache.clear()
HEAD = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
        "Talk Time (min),Sum of Interaction Duration,Call Type,Call Result,"
        "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)\n")
D1, D2 = "2026-10-02", "2026-10-03"
# D1 delivered, carrying a TEAMMATE but not the seat in question. D2 undelivered.
(TMP / ("interactions_%s__inbound.csv" % D1)).write_text(
    HEAD + ("%s,Ariel Ramirez,09:01:00,,Inbound,2.0,120,Outbound,Call connected,"
            "5551112222,5553334444,,0\n" % D1))
cov = A._v6_cx_days_covered([D1, D2], A._TEAM_ROSTERS["inbound"])
ck("a day carrying a teammate counts as COVERED (a zero there is real)",
   D1 in cov, sorted(cov))
ck("a day with no report is NOT covered (a zero there is unread)",
   D2 not in cov, sorted(cov))
ck("no witnesses means do not claim the window is unread",
   A._v6_cx_days_covered([D1, D2], []) == {D1, D2},
   A._v6_cx_days_covered([D1, D2], []))

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
