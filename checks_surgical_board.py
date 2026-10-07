"""Surgical Coordinator KPI: Scheduling and Customer Service as ONE table, with
each row still graded against its own job's bar.

Danny, 2026-10-06: "put the customer service and scheduling KPI trackers on the
same link as the biller one, combine those under one shared table we can call
surgical coordinator KPI".

The trap is the bar. Scheduling's median day is 94 talk minutes; customer
service's is 133, which is above scheduling's STRETCH. One target across both
would mark every scheduler down and every customer-service agent up for doing
their own job properly -- the same defect as pacing a team on another team's
curve. So the combined team is a VIEW over the two rosters, each seat carrying
its own targets and pace curve, and the header names both bars instead of
printing one.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys
import json
import pathlib

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
from billing_report import build_report  # noqa: E402

def by_name(seats, nm):
    return next((x for x in seats if x["name"] == nm), {})


# ---- the combined roster is a view, not a third roster ----
surg = A._TEAM_ROSTERS["surgical"]
names = [x["name"] for x in surg]
sched = [x["name"] for x in A._TEAM_ROSTERS["scheduling"]]
inb = [x["name"] for x in A._TEAM_ROSTERS["inbound"]]
ck("every scheduling seat is on it", all(n in names for n in sched), names)
ck("every customer-service seat is on it", all(n in names for n in inb), names)
ck("nobody is on it twice", len(names) == len(set(names)), names)
BORROWED = {"Vivian Martinez", "Yareth Pavon", "Gabriela Maldonado"}
EXTRA = {"Judith Merlo", "Alex Morales", "Chery Marroquin", "Ana Castro"}
ck("the three billers are OFF it again (Danny, 2026-10-07)", not (BORROWED & set(names)), names)
ck("the RingCX-only people Danny listed are on it", EXTRA <= set(names), names)
ck("and nobody else", set(names) == set(sched) | set(inb) | EXTRA, names)
ck("every surgical seat is listed even at zero",
   all(x.get("always_rank") is True for x in surg), [x["name"] for x in surg if not x.get("always_rank")])
ck("billing itself is untouched by the borrow",
   [x["name"] for x in A._TEAM_ROSTERS["billing"]]
   == ["Vivian Martinez", "Yareth Pavon", "Gabriela Maldonado", "Ana Salazar"],
   [x["name"] for x in A._TEAM_ROSTERS["billing"]])
ck("a RingCX-only newcomer is read from RingCX",
   by_name(surg, "Alex Morales").get("source") == "ringcx", by_name(surg, "Alex Morales"))
ck("and needs no ext_id", by_name(surg, "Alex Morales").get("ext_id") is None,
   by_name(surg, "Alex Morales"))
ck("and is judged on the queue bar, not billing's",
   by_name(surg, "Alex Morales")["targets"]["talk_minutes"]["target"] == 133,
   by_name(surg, "Alex Morales").get("targets"))
ck("scheduling is untouched", len(sched) == 4, sched)
ck("customer service is 5 seats (Ana Salazar is billing-only since 2026-10-07)", len(inb) == 5, inb)
ck("Ana Salazar is NOT on the surgical table", "Ana Salazar" not in names, names)
ck("it is read from RingCX like its parts", A._TEAM_SOURCES["surgical"] == "ringcx",
   A._TEAM_SOURCES.get("surgical"))
ck("it has the name Danny gave it", A.TEAM_LABELS["surgical"] == "Surgical Coordinator KPI",
   A.TEAM_LABELS.get("surgical"))
ck("the landing link shows billing then surgical",
   A.BOARD_TEAMS == ["billing", "surgical"], A.BOARD_TEAMS)

by = {x["name"]: x for x in surg}
ck("a scheduler carries scheduling's bar",
   by["Jorge Mier"]["targets"]["talk_minutes"]["target"] == 94, by["Jorge Mier"].get("targets"))
ck("a customer-service seat carries its own bar",
   by["Ariel Ramirez"]["targets"]["talk_minutes"]["target"] == 133,
   by["Ariel Ramirez"].get("targets"))
ck("each seat knows its group", by["Jorge Mier"]["group"] == "scheduling"
   and by["Ariel Ramirez"]["group"] == "inbound", (by["Jorge Mier"].get("group"),
                                                   by["Ariel Ramirez"].get("group")))
ck("each seat carries its own pace curve",
   by["Jorge Mier"]["default_curve"] is A.TEAM_PACE_CURVES["scheduling"]
   and by["Ariel Ramirez"]["default_curve"] is A.TEAM_PACE_CURVES["inbound"],
   "curves not per group")

# The dedupe cannot be trusted on the real rosters: nobody is on both today, so a
# missing dedupe would pass silently. Put one person on both and rebuild.
_saved_inb = A._TEAM_ROSTERS["inbound"]
A._TEAM_ROSTERS["inbound"] = _saved_inb + [dict(A._TEAM_ROSTERS["scheduling"][0])]
try:
    _dup = A._combined_roster("surgical")
    _n = [x["name"] for x in _dup]
    ck("a person on both rosters appears ONCE", len(_n) == len(set(_n)), _n)
    ck("and keeps the first roster's bar (scheduling)",
       next(x for x in _dup if x["name"] == A._TEAM_ROSTERS["scheduling"][0]["name"])["group"]
       == "scheduling", "group not from first roster")
finally:
    A._TEAM_ROSTERS["inbound"] = _saved_inb

# ---- the roster rebuild keeps the per-seat fields ----
r, _ = A._billing_roster("surgical")
rb = {x["name"]: x for x in r}
ck("_billing_roster keeps group", rb["Jorge Mier"].get("group") == "scheduling", rb["Jorge Mier"])
ck("_billing_roster keeps targets", bool(rb["Jorge Mier"].get("targets")), rb["Jorge Mier"])
ck("_billing_roster keeps the curve", bool(rb["Jorge Mier"].get("default_curve")),
   sorted(rb["Jorge Mier"]))
ck("an ext_id-less RingCX seat survives the roster rebuild",
   "Alex Morales" in rb and rb["Alex Morales"].get("source") == "ringcx", sorted(rb))
ck("_seat_meta carries all three",
   set(A._seat_meta(rb["Jorge Mier"])) >= {"group", "targets", "default_curve"},
   A._seat_meta(rb["Jorge Mier"]))

# ---- the same minutes grade differently per job ----
def day(mins):
    return [{"direction": "Outbound", "result": "Call connected",
             "duration": mins * 60, "start_time": "2026-10-05T14:00:00.000Z"}]

rows = {}
for nm in ("Jorge Mier", "Ariel Ramirez"):
    s_ = rb[nm]
    rows[nm] = {**A._seat_meta(s_), "rows": day(100), "ext": s_["ext"],
                "ext_id": s_["ext_id"], "complete": True, "missing_days": []}
rep = build_report(rows, tz_offset_minutes=240,
                   window={"start": "2026-10-05", "end": "2026-10-05"},
                   targets=A.TEAM_TARGETS["scheduling"])
g = {a["name"]: a for a in rep["ranked"]}
ck("100 minutes is TARGET for a scheduler",
   g["Jorge Mier"]["grades"]["talk_minutes"] == "target", g["Jorge Mier"]["grades"])
ck("100 minutes is only FLOOR for customer service",
   g["Ariel Ramirez"]["grades"]["talk_minutes"] == "floor", g["Ariel Ramirez"]["grades"])
ck("each row carries the bar it was judged against",
   g["Jorge Mier"]["targets"]["talk_minutes"]["target"] == 94
   and g["Ariel Ramirez"]["targets"]["talk_minutes"]["target"] == 133,
   (g["Jorge Mier"]["targets"]["talk_minutes"], g["Ariel Ramirez"]["targets"]["talk_minutes"]))
ck("each row carries its group",
   g["Jorge Mier"]["group"] == "scheduling" and g["Ariel Ramirez"]["group"] == "inbound",
   (g["Jorge Mier"].get("group"), g["Ariel Ramirez"].get("group")))
ck("the report says the targets are mixed", rep["mixed_targets"] is True, rep["mixed_targets"])
ck("and names both bars",
   sorted((x["group"], x["targets"]["talk_minutes"]["target"]) for x in rep["target_groups"])
   == [("inbound", 133), ("scheduling", 94)], rep["target_groups"])

# An ordinary board must NOT start reading as mixed.
rep1 = build_report({"Jorge Mier": {"rows": day(100), "ext": "221", "ext_id": 1,
                                    "complete": True}},
                    tz_offset_minutes=240,
                    window={"start": "2026-10-05", "end": "2026-10-05"},
                    targets=A.TEAM_TARGETS["scheduling"])
ck("a single-job board is not mixed", rep1["mixed_targets"] is False, rep1["mixed_targets"])
ck("and has one target group", len(rep1["target_groups"]) == 1, rep1["target_groups"])
ck("a seat with no bar of its own uses the board's",
   rep1["ranked"][0]["targets"]["talk_minutes"]["target"] == 94, rep1["ranked"][0]["targets"])

# ---- one build, two platforms ----
asked_ex, asked_cx = [], []
_sv = (A._v6_fetch_ringex, A._v6_cx_rows_for_team, A._v6_fetch_sms, A._v6_finish)
A._v6_fetch_ringex = lambda seats, d_, lt, tz: (asked_ex.extend(x["name"] for x in seats) or
    ({x["name"]: {"rows": [], "ext": x["ext"], "ext_id": x["ext_id"], "complete": True,
                  "missing_days": []} for x in seats}, {"cached": 0, "fetched": 0, "missing": 0}))
A._v6_cx_rows_for_team = lambda t, d_, seats: (asked_cx.extend(x["name"] for x in seats) or
    ({x["name"]: [] for x in seats}, len(d_), {}))
_sms_calls = []
A._v6_fetch_sms = lambda r_, d_, lt, tz: (_sms_calls.append(len(r_)), ({}, {}))[1]
_got = {}
A._v6_finish = lambda rows_by_agent, *a, **k: _got.setdefault("rows", rows_by_agent)
try:
    A._v6_build("2026-10-05", "2026-10-05", 240, "2026-10-05", team="surgical")
finally:
    A._v6_fetch_ringex, A._v6_cx_rows_for_team, A._v6_fetch_sms, A._v6_finish = _sv
ck("nobody on the surgical table is asked of RingEX", asked_ex == [], asked_ex)
ck("everyone is asked of RingCX, in one read",
   set(asked_cx) == set(sched) | set(inb) | EXTRA, asked_cx)
ck("every seat reaches the report", len(_got.get("rows", {})) == 13, len(_got.get("rows", {})))
# Danny, 2026-10-07: "dont show sms performance for surgical coordinators".
# Off means not READ either: no message-store budget for a board that hides it.
ck("the surgical build never reads SMS", _sms_calls == [], _sms_calls)

# ---- SMS for a seat with no extension is unknown, not zero ----
_sv_day = A._v6_fetch_sms_day
A._v6_fetch_sms_day = lambda eid, day, tz: ([], True, "sms")
try:
    sms, _ = A._v6_fetch_sms(A._billing_roster("surgical")[0], ["2026-10-05"], "2026-10-05", 240)
finally:
    A._v6_fetch_sms_day = _sv_day
ck("no extension -> SMS not read", sms.get("Alex Morales", {}).get("complete") is False,
   sms.get("Alex Morales"))
ck("and the reason is stated", "extension" in (sms.get("Alex Morales", {}).get("note") or ""),
   sms.get("Alex Morales"))

# ---- presence is a RingEX fact; withheld for a RingCX team ----
A.app.config["TESTING"] = True
c = A.app.test_client()
_real = A._ringcx.agent_statuses_with_age
A._ringcx.agent_statuses_with_age = lambda: ([], {"age_seconds": 1.0, "stale": False,
                                                  "never_read": False, "note": None,
                                                  "ttl_seconds": 60})
try:
    j = c.get("/api/v6/presence?team=surgical").get_json()
    ck("presence is withheld for the all-RingCX surgical table",
       j.get("withheld") is True and j.get("seats") == [], j)
    ja = c.get("/api/v6/presence?team=scheduling").get_json()
    ck("an all-RingCX team is withheld with the reason",
       ja.get("withheld") is True and "RingCX" in (ja.get("note") or ""), ja)
    jb = c.get("/api/v6/presence?team=billing").get_json()
    ck("billing presence is not withheld on that ground",
       "RingCX" not in (jb.get("note") or ""), jb.get("note"))
finally:
    A._ringcx.agent_statuses_with_age = _real

# ---- the pages ----
html = c.get("/v6").get_data(as_text=True)
ck("the landing page carries the board list",
   'var BOARD_TEAMS = ["billing", "surgical"]' in html, "BOARD_TEAMS not in /v6")
ck("the combined team has its own page",
   c.get("/surgical-coordinator").status_code == 200,
   c.get("/surgical-coordinator").status_code)
one = c.get("/surgical-coordinator").get_data(as_text=True)
ck("that page pins ONE board", 'var FIXED = "surgical"' in one, "FIXED not pinned")
ck("rows carry NO job label (removed at Danny's request)",
   "groupChip" not in html and "GROUP_LABELS" not in html, "job-label code still in the page")
ck("the first board keeps the page's own heading (billing looks as it did)",
   "slot === slotId(BOARDS[0])" in html and "(i ? '<h2 class=\"bt\"" in html,
   "first board no longer owns the h1 / gets a second heading")
ck("row ids are scoped to the board, so two boards do not collide",
   "var id = slot + '_d' + i;" in html and "var id = 'd' + i;" not in html,
   "row ids not scoped by slot")
ck("a mixed table prints no single headline target",
   "if (d.mixed_targets" in html, "mixed-target guard missing")

# ---- the report endpoint serves it ----
# /api/v6/report answers 503 before anything else when RingCentral is not
# configured, and it is not configured on this machine. The combined team is
# read from delivered RingCX files and never touches RingCentral, so the gate is
# lifted for this one call. `configured` is a read-only property, hence the
# class-level override; it is restored whatever happens.
_cls = type(A._ringcx)
_real_cfg = _cls.__dict__.get("configured")
_real_sms = A._v6_fetch_sms
_cls.configured = property(lambda self: True)
A._v6_fetch_sms = lambda roster_, days_, lt, tz: ({}, {})
try:
    rr = c.get("/api/v6/report?team=surgical&start=2026-07-29&end=2026-07-29&tz=240")
    jj = rr.get_json() or {}
    ck("/api/v6/report serves the combined team", rr.status_code == 200, rr.status_code)
    ck("under its own label", jj.get("team_label") == "Surgical Coordinator KPI",
       jj.get("team_label"))
    all_rows = (jj.get("ranked", []) + jj.get("silent", []) + jj.get("stalled", [])
                + jj.get("unknown", []))
    ck("with every seat present", len(all_rows) == 13, len(all_rows))
    ck("the surgical report says SMS is off", jj.get("sms_enabled") is False, jj.get("sms_enabled"))
    ck("no surgical row carries SMS", all(not a.get("sms") for a in all_rows),
       [a["name"] for a in all_rows if a.get("sms")])
    ck("no SMS note on the surgical board",
       not [w for w in jj.get("warnings", []) if "sms" in (w.get("kind") or "")],
       [w.get("kind") for w in jj.get("warnings", [])])
    jb = c.get("/api/v6/report?team=billing&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
    ck("billing still has SMS on", jb.get("sms_enabled") is True, jb.get("sms_enabled"))
    ck("the page hides the chip when a board has SMS off",
       "sms_enabled === false) return ''" in html, "smsChip not gated on sms_enabled")
    ck("and every one of them in the ranked table, none in a footnote",
       len(jj.get("ranked", [])) == 13, (len(jj.get("ranked", [])), [a["name"] for a in jj.get("silent", [])]))
finally:
    A._v6_fetch_sms = _real_sms
    if _real_cfg is not None:
        _cls.configured = _real_cfg
    else:
        delattr(_cls, "configured")

# ---- /api/build says whether today's report names match the rosters ----
import tempfile, datetime  # noqa: E402
_inbox = pathlib.Path(tempfile.mkdtemp()) / "inbox"; _inbox.mkdir()
_real_inbox = A.RINGCX_INBOX_DIR
A.RINGCX_INBOX_DIR = _inbox
A._inbox_parse_cache.clear()
_tz = -int(os.environ.get("TZ_OFFSET_HOURS", "-4")) * 60
_today = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=_tz)).date().isoformat()
HEAD = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
        "Talk Time (min),Sum of Interaction Duration,Call Type,Call Result,"
        "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)\n")
def _row(name, minute):
    return ("%s,%s,09:%02d:00,,Inbound,2.0,120,Outbound,Call connected,"
            "5551112222,5553334444,,0\n" % (_today, name, minute))
(_inbox / ("interactions_%s__inbound.csv" % _today)).write_text(
    HEAD + _row("Alex Morales", 1) + _row("Alex Morales", 2)
    + _row("Nobody Known", 3) + _row("", 4))
try:
    b = c.get("/api/build").get_json().get("ringcx_inbox", {})
    m = b.get("roster_match_today", {}).get("surgical", {})
    ck("/api/build counts surgical seats seen today", m.get("seats_seen_today") == 1, m)
    ck("and the rows they account for", m.get("rows_matched_today") == 2, m)
    ck("and knows the roster size", m.get("seats") == 13, m)
    ck("a name on no roster is counted, not named",
       b.get("today_agents_on_no_roster") == 1, b.get("today_agents_on_no_roster"))
    # Blank-agent rows are dropped by the parser before this point, so there is
    # deliberately NO "rows with no agent" figure: it could only ever be 0.
    ck("no always-zero queue-traffic figure is published",
       "today_rows_with_no_agent" not in b, sorted(b))
    # A person on two rosters (scheduling AND surgical) was listed twice among
    # the absent; the fixture's day carries only Alex Morales, so everyone else
    # on the RingCX rosters is absent, each exactly once.
    ja = c.get("/api/v6/cx-agents?days=1").get_json() or {}
    _absent = ja.get("ringcx_roster_absent_from_reports", [])
    ck("the absent list names each person once", len(_absent) == len(set(_absent)), _absent)
    ck("and does not name the one who was seen", "Alex Morales" not in _absent, _absent)
    ck("no agent NAME leaks through the open endpoint",
       "Alex Morales" not in json.dumps(b) and "Nobody Known" not in json.dumps(b), b)
finally:
    A.RINGCX_INBOX_DIR = _real_inbox
    A._inbox_parse_cache.clear()

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
