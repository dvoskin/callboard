"""A billing seat whose CALLS are on RingCX must rank from RingCX, and a seat
taken off the board must not reappear below it.

Danny, 2026-10-05: "pull in Ana's CX data and list it at 0.0 if its not pulled
but put her on the chart, and take Andrea off from there."

Four things can go wrong, and each has a case here:
  1. _billing_roster REBUILDS every seat from three keys, so source/always_rank
     are easy to drop -- which turns a RingCX seat back into a RingEX one and
     scores her on a handset that has been dead since 2026-08-11.
  2. A zero-call seat is normally held OUT of the ranking by design. always_rank
     has to override that, and ONLY for the seat that asked for it.
  3. A ranked 0.0 must still say why it is zero, or it reads as a measured day.
  4. Dropping a seat from the roster is not the same as taking it off the board:
     her sheet tab still maps to her, so she returns as an untracked collector.
"""
import sys

errors = []
def ck(label, cond, got=None):
    if not cond:
        errors.append("%s -- got %r" % (label, got))

import app as A
from billing_report import build_report

# ---- 1. the roster carries the optional keys through ----
roster, meta = A._billing_roster("billing")
by = {s["name"]: s for s in roster}
ck("Andrea Pleasant is off the billing roster", "Andrea Pleasant" not in by, sorted(by))
ck("Ana Salazar is on the billing roster", "Ana Salazar" in by, sorted(by))
ana = by.get("Ana Salazar", {})
ck("Ana's calls are sourced from RingCX", ana.get("source") == "ringcx", ana)
ck("Ana witnesses coverage from inbound", ana.get("source_team") == "inbound", ana)
ck("Ana is ranked even at zero", ana.get("always_rank") is True, ana)
ck("a plain seat keeps the three-key shape",
   set(by.get("Vivian Martinez", {})) == {"name", "ext_id", "ext"},
   by.get("Vivian Martinez"))

# ---- 2 & 3. always_rank ranks a zero seat, and says why ----
WIN = {"start": "2026-10-01", "end": "2026-10-05"}
rep = build_report({
    "Ana Salazar": {"rows": [], "ext": "271", "ext_id": 1,
                    "complete": True, "call_source": "RingCX", "always_rank": True},
    "Vivian Martinez": {"rows": [], "ext": "137", "ext_id": 2, "complete": True},
}, tz_offset_minutes=240, window=WIN)
names = [a["name"] for a in rep["ranked"]]
ck("the zero seat that asked for it is RANKED", names == ["Ana Salazar"], names)
ck("an ordinary zero seat is still held out",
   [a["name"] for a in rep["silent"]] == ["Vivian Martinez"], rep["silent"])
ck("the ranked seat is not ALSO listed as silent",
   "Ana Salazar" not in [a["name"] for a in rep["silent"]], rep["silent"])
ranked_ana = rep["ranked"][0] if rep["ranked"] else {}
ck("her row shows 0.0 talk, not a blank",
   ranked_ana.get("per_day", {}).get("talk_minutes") == 0.0,
   ranked_ana.get("per_day"))
ck("her row names the platform", ranked_ana.get("call_source") == "RingCX",
   ranked_ana.get("call_source"))
kinds = [w["kind"] for w in rep["warnings"]]
ck("a ranked zero still raises a finding", "ranked_at_zero" in kinds, kinds)
msg = " ".join(w["message"] for w in rep["warnings"] if w["kind"] == "ranked_at_zero")
ck("and the finding says the data WAS read", "carries none" in msg, msg)

# The other half: not pulled must not read as a worked zero.
rep2 = build_report({
    "Ana Salazar": {"rows": [], "ext": "271", "ext_id": 1, "complete": False,
                    "missing_days": ["2026-10-02", "2026-10-05"],
                    "call_source": "RingCX", "always_rank": True},
}, tz_offset_minutes=240, window=WIN)
ck("unpulled still ranks her", [a["name"] for a in rep2["ranked"]] == ["Ana Salazar"],
   rep2["ranked"])
m2 = " ".join(w["message"] for w in rep2["warnings"] if w["kind"] == "ranked_at_zero")
ck("unpulled is called NOT READ, not no-calls", "not read" in m2, m2)

# ---- 5. the split itself: who is asked of WHICH platform ----
# The riskiest line in this change. If Ana is left in the RingEX half she is
# billed against a handset dead since 2026-08-11 -- 2-9 unanswered dials a day,
# which is exactly the reading that made her look like a failing agent.
asked_ex, asked_cx = [], []

def _fake_ex(seats, days, local_today, tz):
    asked_ex.extend(x["name"] for x in seats)
    return ({x["name"]: {"rows": [], "ext": x["ext"], "ext_id": x["ext_id"],
                         "complete": True, "missing_days": []} for x in seats},
            {"cached": 0, "fetched": 0, "missing": 0})

def _fake_cx_rows(team, days, seats):
    asked_cx.extend(x["name"] for x in seats)
    return ({x["name"]: [] for x in seats}, len(days), {})

_real = (A._v6_fetch_ringex, A._v6_cx_rows_for_team, A._v6_fetch_sms, A._v6_finish)
seen = {}
A._v6_fetch_ringex = _fake_ex
A._v6_cx_rows_for_team = _fake_cx_rows
A._v6_fetch_sms = lambda roster, days, lt, tz: ({}, {})
A._v6_finish = lambda rows_by_agent, *a, **k: seen.setdefault("rows", rows_by_agent)
try:
    A._v6_build("2026-10-01", "2026-10-02", 240, "2026-10-02", team="billing")
finally:
    (A._v6_fetch_ringex, A._v6_cx_rows_for_team,
     A._v6_fetch_sms, A._v6_finish) = _real

ck("Ana is NOT asked of RingEX (her line is dead)", "Ana Salazar" not in asked_ex, asked_ex)
ck("Ana IS asked of RingCX", asked_cx == ["Ana Salazar"], asked_cx)
ck("the RingEX seats are still asked of RingEX",
   sorted(asked_ex) == ["Gabriela Maldonado", "Vivian Martinez", "Yareth Pavon"], asked_ex)
got = seen.get("rows", {})
ck("every seat reaches the report", sorted(got) == sorted(
   ["Ana Salazar", "Gabriela Maldonado", "Vivian Martinez", "Yareth Pavon"]), sorted(got))
ck("Ana's half is labelled RingCX",
   got.get("Ana Salazar", {}).get("call_source") == "RingCX", got.get("Ana Salazar"))
ck("Ana's half carries always_rank",
   got.get("Ana Salazar", {}).get("always_rank") is True, got.get("Ana Salazar"))
ck("a RingEX seat is not labelled with a source",
   not got.get("Vivian Martinez", {}).get("call_source"), got.get("Vivian Martinez"))

# ---- 4. off the board means off the footnote too ----
ck("Andrea is excluded from the collectors footnote",
   "Andrea Pleasant" in A.BILLING_BOARD_EXCLUDE, A.BILLING_BOARD_EXCLUDE)

# ---- the coverage witness: her absence vs her report's absence ----
import tempfile, pathlib, datetime
TMP = pathlib.Path(tempfile.mkdtemp()) / "inbox"; TMP.mkdir(parents=True)
A.RINGCX_INBOX_DIR = TMP
A._inbox_parse_cache.clear()
HEAD = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
        "Talk Time (min),Sum of Interaction Duration,Call Type,Call Result,"
        "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)\n")
def row(day, name):
    return ("%s,%s,09:01:00,UC Call,UC,2.0,120,Outbound,Call connected,"
            "5551112222,5553334444,,0\n" % (day, name))
D1, D2 = "2026-10-02", "2026-10-03"
# D1 delivered, carrying a TEAMMATE but not Ana. D2 not delivered at all.
(TMP / ("interactions_%s__inbound.csv" % D1)).write_text(HEAD + row(D1, "Ariel Ramirez"))
cov = A._v6_cx_days_covered([D1, D2], A._TEAM_ROSTERS["inbound"])
ck("a day carrying a teammate counts as COVERED (her zero is real)", D1 in cov, sorted(cov))
ck("a day with no report is NOT covered (her zero is unread)", D2 not in cov, sorted(cov))
ck("no witnesses means do not claim the window is unread",
   A._v6_cx_days_covered([D1, D2], []) == {D1, D2},
   A._v6_cx_days_covered([D1, D2], []))

for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
