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
import time

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
EXTRA = {"Judith Merlo", "Alex Morales"}
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
ck("and carries the RingEX extension the RingCX agent record names (rcUserId), for SMS",
   str(by_name(surg, "Alex Morales").get("ext_id")) == "431142034" and by_name(surg, "Alex Morales").get("ext") == "208",
   by_name(surg, "Alex Morales"))
ck("Chery Marroquin, Luisa Perez and Ana Castro are off it for now (Danny, 2026-10-08)",
   not by_name(surg, "Luisa Perez") and not by_name(surg, "Chery Marroquin") and not by_name(surg, "Ana Castro"), [x["name"] for x in surg])
ck("and is judged on the queue bar, not billing's",
   by_name(surg, "Alex Morales")["targets"]["talk_minutes"]["target"] == 133,
   by_name(surg, "Alex Morales").get("targets"))
ck("scheduling is the two confirmed seats (Alanis, Sarahi dropped 2026-10-07: in no system)", sched == ["Jorge Mier", "Oscar Caballero"], sched)
ck("customer service is the two confirmed seats (Antonio, Kevin, Angi dropped; Ana Salazar billing-only)", inb == ["Johana Duron", "Ariel Ramirez"], inb)
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
ck("every seat reaches the report", len(_got.get("rows", {})) == 6, len(_got.get("rows", {})))
# Danny, 2026-10-07: "dont show sms performance for surgical coordinators".
# Off means not READ either: no message-store budget for a board that hides it.
# Danny, 2026-10-07 evening: SMS is back ON for surgical ("show inbound
# outbound sms for the surgical coordinators as well, sending from ringex").
ck("the surgical build reads SMS for its roster", _sms_calls == [6], _sms_calls)

# ---- SMS for a seat with no extension is unknown, not zero ----
_sv_day = A._v6_fetch_sms_day
A._v6_fetch_sms_day = lambda eid, day, tz: ([], True, "sms")
try:
    # every real surgical seat now has its extension (resolved 2026-10-07), so
    # the ext-less case is a synthetic seat -- the mechanism is what is checked
    sms, _ = A._v6_fetch_sms([{"name": "Alex Morales", "source": "ringcx", "ext": "", "ext_id": None}],
                             ["2026-10-05"], "2026-10-05", 240)
finally:
    A._v6_fetch_sms_day = _sv_day
ck("no extension -> SMS not read", sms.get("Alex Morales", {}).get("complete") is False,
   sms.get("Alex Morales"))
ck("and the reason is stated", "extension" in (sms.get("Alex Morales", {}).get("note") or ""),
   sms.get("Alex Morales"))
# ---- a seat past this refresh's budget is "not read YET", with that reason -- never "refused" ----
_sv_day, _sv_budget = A._v6_fetch_sms_day, A._V6_SMS_BUDGET
A._v6_fetch_sms_day = lambda eid, day, tz: ([], True, "sms", None)
A._V6_SMS_BUDGET = 1
try:
    sms, _ = A._v6_fetch_sms([{"name": "Judith Merlo", "source": "ringcx", "ext": "185", "ext_id": 1076638035},
                              {"name": "Ana Castro", "source": "ringcx", "ext": "180", "ext_id": 695481035}],
                             ["2026-10-05"], "2026-10-05", 240)
    ck("the seat inside the budget is read", sms["Judith Merlo"]["complete"] is True and sms["Judith Merlo"]["note"] is None, sms["Judith Merlo"])
    ck("the seat past it is not read YET, and says the budget is why",
       sms["Ana Castro"]["complete"] is False and "budget" in (sms["Ana Castro"]["note"] or "") and "next refresh" in sms["Ana Castro"]["note"], sms["Ana Castro"])
    A._v6_sms_today_cache.clear()
    A._V6_SMS_BUDGET = 5
    A._v6_fetch_sms_day = lambda eid, day, tz: ([], False, "sms", "RingEX is rate limiting the message store (HTTP 429); this seat's SMS figures are incomplete for the window.")
    sms, _ = A._v6_fetch_sms([{"name": "Ana Castro", "source": "ringcx", "ext": "180", "ext_id": 695481035}], ["2026-10-05"], "2026-10-05", 240)
    ck("a real failure carries the store's own words", "429" in (sms["Ana Castro"]["note"] or ""), sms["Ana Castro"])
    rep_ = build_report({"Ana Castro": {"rows": [], "ext": "180", "ext_id": 695481035, "complete": True, "missing_days": []}},
                        tz_offset_minutes=240, window={"start": "2026-10-05", "end": "2026-10-05"}, sms_by_agent=sms)
    a_ = next(x for x in rep_["ranked"] + rep_["silent"] + rep_["stalled"] + rep_["unknown"] if x["name"] == "Ana Castro")
    ck("and the report hands that reason to the page", "429" in ((a_.get("sms") or {}).get("note") or ""), a_.get("sms"))
finally:
    A._v6_fetch_sms_day, A._V6_SMS_BUDGET = _sv_day, _sv_budget
    A._v6_sms_today_cache.clear()
_tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html"), encoding="utf-8").read()
ck("the page never says 'refused' on its own", "refused the message store" not in _tpl and "a.sms.note ? esc(a.sms.note)" in _tpl, "guessed wording still in the template")

# ---- presence is a RingEX fact; withheld for a RingCX team ----
A.app.config["TESTING"] = True
c = A.app.test_client()
_real = A._ringcx.agent_statuses_with_age
A._ringcx.agent_statuses_with_age = lambda: ([], {"age_seconds": 1.0, "stale": False,
                                                  "never_read": False, "note": None,
                                                  "ttl_seconds": 60})
try:
    _rc = A._ringcx.active_calls_with_status
    A._ringcx.active_calls_with_status = lambda: ([], {"ok": False, "note": "no RingCX creds here", "read_at": 0, "http_error": None})
    A._cx_active_cache.update(at=0.0, calls=[], meta=None)
    try:
        j = c.get("/api/v6/presence?team=surgical").get_json()
    finally:
        A._ringcx.active_calls_with_status = _rc
        A._cx_active_cache.update(at=0.0, calls=[], meta=None)
    ck("every surgical seat is reported, from RingCX, not withheld as a team",
       len(j.get("seats", [])) == 6 and all(x.get("source") == "ringcx" for x in j["seats"]), j)
    ck("when RingCX cannot be read each seat is NOT READ, never idle",
       all(x.get("state") == "unknown" for x in j["seats"]) and j.get("withheld") is True, j)
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
ck("the landing page is named as a dashboard over both boards",
   ('var LANDING_TITLE = "Billing \\u0026 Surgical Coordinator Dashboard"' in html
    or 'var LANDING_TITLE = "Billing & Surgical Coordinator Dashboard"' in html)
   and A.LANDING_TITLE == "Billing & Surgical Coordinator Dashboard",
   "landing title missing")
ck("every board on it has its own plain heading and summary line",
   "'<h2 class=\"bt\" id=\"bt_' + slotId(t) + '\">" in html and "(i ? '<h2" not in html, "boards not uniformly headed")
ck("a single-board page is still titled by its board", "if (d.team_label && !MULTI_BOARD && !AGENT) {" in html, "single-board title lost")
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
    ck("with every seat present", len(all_rows) == 6, len(all_rows))
    ck("the surgical report says SMS is on", jj.get("sms_enabled") is True, jj.get("sms_enabled"))
    ck("sales stays off", A._team_sms_enabled("sales") is False, A._team_sms_enabled("sales"))
    jb = c.get("/api/v6/report?team=billing&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
    ck("billing still has SMS on", jb.get("sms_enabled") is True, jb.get("sms_enabled"))
    ck("the page hides the chip when a board has SMS off",
       "sms_enabled === false) return ''" in html, "smsChip not gated on sms_enabled")
    ck("and every one of them in the ranked table, none in a footnote",
       len(jj.get("ranked", [])) == 6, (len(jj.get("ranked", [])), [a["name"] for a in jj.get("silent", [])]))
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
    ck("and knows the roster size", m.get("seats") == 6, m)
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

# ---- inbound and outbound shown separately on the queue tables ----
# Danny, 2026-10-07: "show inbound and outbound interactions for each agent not
# just outbound on the surgical coordinator table".
def _r(direction, result):
    return {"direction": direction, "result": result, "duration": 60,
            "start_time": "2026-10-05T14:00:00.000Z"}
rd = build_report({"Judith Merlo": {"rows": [
    _r("Inbound", "Accepted"), _r("Inbound", "Missed"),
    _r("Outbound", "Call connected"), _r("Outbound", "No Answer"), _r("Outbound", "No Answer"),
], "ext": "", "ext_id": None, "complete": True}}, tz_offset_minutes=240,
    window={"start": "2026-10-05", "end": "2026-10-05"})
pdj = rd["ranked"][0]["per_day"]
ck("inbound interactions per day counts answered AND missed", pdj.get("inbound") == 2.0, pdj)
ck("inbound answered is carried separately", pdj.get("inbound_answered") == 1.0, pdj)
ck("outbound interactions per day counts every dial", pdj.get("outbound") == 3.0, pdj)
ck("the KPI's handled count is unchanged (dials + inbound answered)", pdj.get("calls") == 4.0, pdj)
ck("the surgical report asks the page to split by direction", jj.get("split_direction") is True, jj.get("split_direction"))
ck("billing does not", jb.get("split_direction") is False, jb.get("split_direction"))

ck("zero-call seats collapse on the live day", "r r-quiet" in html and "no calls yet today" in html, "quiet-row branch missing")
ck("the In / Out cell, header and panel lines exist", "'In / Out' : 'Calls'" in html and "line('Inbound'" in html, "direction UI missing")
ck("data notes fold behind one line", '<details class="notes"><summary>' in html, "notes not folded")

ck("Conn and the long-call column are off the row", "cell('cn'" not in html and "cell('lg'" not in html
   and 'class="a-cn"' not in html, "Conn/long-call cells still on the row")
ck("but still in the expand panel", "['connected', 'Connected', '']" in html and "['long_calls', longLbl, '']" in html,
   "panel lost Connected / long calls")
ck("no grid area still names the removed columns", " cn lg" not in html and '"c  cn lg"' not in html, "stale grid areas")

# ---- Zoho CRM activities created, on each surgical row ----
_seen_window = {}
_real_users, _real_bd = A._zoho.list_users, A._zoho.activity_breakdown
A._zoho.list_users = lambda: {"u-judith": "Judith", "u-oscar": "Oscar Caballero"}
def _stub_bd(s_, e_, ids, start_date=None, end_date=None):
    _seen_window.update(start=s_, end=e_, ids=sorted(ids), sd=start_date, ed=end_date)
    _seen_window.setdefault("all", []).append(sorted(ids))
    return {"u-judith": {"calls": {"created": 3, "due": 5, "completed": 4, "overdue": 1},
                         "tasks": {"created": 2, "due": 6, "completed": 5, "open": 1}}}
A._zoho.activity_breakdown = _stub_bd
_real_jbd = A._zoho.journey_breakdown
A._zoho.journey_breakdown = lambda ids, s_, e_, anchor: {"u-judith": {"active": 207, "soon": {"total": 15, "not_ready": 6, "labs": 5, "mc": 1, "balance": 4},
                                                                        "journey_calls": {"due": 14, "scheduled": 6, "overdue": 1, "completed": 7},
                                                                        "queue": {"to_schedule": 2, "escalated": 1, "oldest_wait_days": 5, "calls_overdue": 9, "calls_scheduled_today": 4, "tasks_open": 3}}}
A._crm_bd_cache.clear(); A._crm_bd_inflight.clear()
A._CRM_ASYNC = False          # the checks read inline; production reads on a thread
A._v6_cache.clear()          # the earlier request cached rows without CRM on them
try:
    _cls.configured = property(lambda self: True)
    A._v6_fetch_sms = lambda r_, d_, lt, tz: ({}, {})
    jc = c.get("/api/v6/report?team=surgical&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
    jb2 = c.get("/api/v6/report?team=billing&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
finally:
    A._zoho.list_users, A._zoho.activity_breakdown = _real_users, _real_bd
    A._crm_bd_cache.clear(); A._crm_bd_inflight.clear()
    A._CRM_ASYNC = True
    if _real_cfg is not None: _cls.configured = _real_cfg
    else: delattr(_cls, "configured")
    A._v6_fetch_sms = _real_sms
rows_c = {a["name"]: a for k in ("ranked", "silent", "stalled", "unknown") for a in jc.get(k, [])}
ck("the surgical report carries CRM", jc.get("crm_enabled") is True, jc.get("crm_enabled"))
ck("the CRM window is passed as local-day DATETIMES (COQL rejects bare dates)",
   _seen_window.get("start", "").startswith("2026-07-29T00:00:00") and _seen_window.get("end", "").startswith("2026-07-29T23:59:59"),
   _seen_window)
ck("live-day seats with no calls and no CRM activity are not tabulated but denoted",
   "not active today" in html and "var quietToday = !((a.totals || {}).calls) && !a.crm_created && !a.collected_total;" in html
   and "quietSeats.forEach" in html, "no live-day quiet handling")
jcrm = rows_c.get("Judith Merlo", {}).get("crm")
ck("her Journeys caseload rides on the same read", (jcrm or {}).get("journeys", {}).get("active") == 207
   and (jcrm or {}).get("journeys", {}).get("soon", {}).get("not_ready") == 6, (jcrm or {}).get("journeys"))
ck("Judith Merlo is attributed through her Zoho name 'Judith' (the alias)",
   A._CRM_NAME_ALIASES.get("Judith Merlo") == "Judith" and jcrm is not None, jcrm)
ck("a CRM user has the full breakdown on her row",
   {k: v for k, v in (jcrm or {}).items() if k != 'journeys'} == {"calls": {"created": 3, "due": 5, "completed": 4, "overdue": 1},
            "tasks": {"created": 2, "due": 6, "completed": 5, "open": 1}}, jcrm)
ck("a CRM user with nothing in the window has real zeros",
   rows_c.get("Oscar Caballero", {}).get("crm", {}).get("calls", {}).get("completed") == 0, rows_c.get("Oscar Caballero", {}).get("crm"))
ck("someone who is not a CRM user by name is None, not 0", "crm" in rows_c.get("Jorge Mier", {}) and rows_c["Jorge Mier"]["crm"] is None,
   rows_c.get("Jorge Mier", {}).get("crm", "missing"))
ck("and is named in the meta so the gap is visible", "Jorge Mier" in (jc.get("crm_meta") or {}).get("not_crm_users", []),
   jc.get("crm_meta"))
# billing now reads CRM too, so the LAST call may be billing's (no stub users match it: []);
# the surgical call must have asked for exactly its two CRM users and nobody else
ck("only the board's CRM ids are queried", ["u-judith", "u-oscar"] in _seen_window.get("all", [])
   and all(set(x) <= {"u-judith", "u-oscar"} for x in _seen_window.get("all", [])), _seen_window.get("all"))
ck("tasks due uses DATE bounds, calls use datetimes",
   _seen_window.get("sd") == "2026-07-29" and _seen_window.get("ed") == "2026-07-29", _seen_window)
ck("the row chip (when shown) reads completed counts", "CRM <b>' + c.completed + '</b> calls" in html, "chip not on completed counts")
ck("but the chip is hidden in the collapsed row for now", "var SHOW_CRM_CHIP = false;" in html and "if (!SHOW_CRM_CHIP || !D.crm_enabled) return '';" in html,
   "CRM chip not gated off")
ck("the panel shows CRM as done-of-due with overdue and logged (Danny, evening 2026-10-07)",
   "line('CRM calls', cc.completed + ' of ' + cc.due + ' done'" in html and "' overdue</b>'" in html and "' logged'" in html
   and "blk('CRM', crmLines" in html, "CRM performance lines missing")
ck("and says not read, with the reason, for a non-CRM name", "line('CRM', 'not read'" in html, "no not-read line")
ck("billing carries CRM too since 2026-10-08 (the planner's billing follow-up calls are theirs)", jb2.get("crm_enabled") is True, jb2.get("crm_enabled"))
ck("sales does not", A._TEAM_CRM.get("sales") is None, A._TEAM_CRM)
ck("collections sit in their own block on the right", "blk('Collections', collLine" in html, "collections block missing")
ck("the page shows the CRM chip only on boards that carry it", "function crmChip" in html and "!D.crm_enabled) return ''" in html,
   "crmChip missing or ungated")

ck("a table of only quiet seats is not an empty board",
   "&& !unknown.length && !quietSeats.length) {" in html, "empty-state check ignores quiet seats")
ck("quiet seats count in the headline seat total",
   "unknown.length + quietSeats.length;" in html, "seatsAll excludes quiet seats")

ck("a quiet seat with no shift on file says so", "'no shift on file'" in html, "quiet line silent about a missing shift")

ck("on the live day unread seats fold into the quiet lines",
   "unknown.forEach(function (a) { quietSeats.push(a); });" in html and "unknown = [];" in html,
   "unread seats still a paragraph each on the live day")

ck("the SMS chip is back on the collapsed row (Danny, 2026-10-07 evening) and the panel line stays",
   "var SHOW_SMS_CHIP = true;" in html and "smsLine = line('SMS'" in html, "SMS chip off / panel line missing")
ck("the live strip exists and agrees with the table's quiet count",
   "class=\"now\"" in html and "nQuiet = quietSeats.length" in html, "live strip missing")
ck("the panel is grouped", "blk('Quality'" in html and "grp('Schedule &amp; line')" in html, "panel not grouped")

ck("on the live day the summary line folds under the strip",
   "$(subId).innerHTML = d.live ? '' : sub;" in html and '<details class="notes subfold"><summary>Summary</summary>' in html,
   "summary line not folded on the live day")

ck("a seat that collected money today is not folded away as inactive",
   "&& !a.collected_total;" in html, "collections ignored by the fold")
ck("the Collected column survives when the money sits on folded seats",
   "concat(ranked, silent, stalled, unknown, quietSeats)" in html, "hasColl ignores folded seats")
ck("the strip and heading say Not Active, not Quiet",
   "</b>Not Active</span>" in html and "Not active today</div>" in html and "</b>Quiet</span>" not in html, "still says Quiet")

# ---- the dashboard's own link ----
r1 = c.get("/billing-surgical")
ck("/billing-surgical serves the dashboard", r1.status_code == 200, r1.status_code)
h1 = r1.get_data(as_text=True)
ck("with both boards", 'var BOARD_TEAMS = ["billing", "surgical"]' in h1 and 'var FIXED = ""' in h1, "not the two-board landing")
ck("under the dashboard title", "Surgical Coordinator Dashboard" in h1, "title missing")
ck("a bad share token is a 404, not a page", c.get("/billing-surgical/board?k=wrong").status_code == 404,
   c.get("/billing-surgical/board?k=wrong").status_code)
_tok = A._v6_token_ok
A._v6_token_ok = lambda: True
try:
    r2 = c.get("/billing-surgical/board?k=ok")
    h2 = r2.get_data(as_text=True)
    ck("the share form renders both boards without a login",
       r2.status_code == 200 and 'var BOARD_TEAMS = ["billing", "surgical"]' in h2 and "var SHARE = true" in h2, (r2.status_code, "SHARE" in h2))
finally:
    A._v6_token_ok = _tok

# ---- the page's meta title ----
ck("the dashboard pages carry the dashboard name in <title>",
   "<title>Billing &amp; Surgical Coordinator Dashboard</title>" in h1
   and "<title>Billing &amp; Surgical Coordinator Dashboard</title>" in c.get("/v6").get_data(as_text=True), "landing <title> wrong")
ck("and in the h1 before any script runs",
   '<h1 id="title">Billing &amp; Surgical Coordinator Dashboard</h1>' in h1, "landing h1 wrong")
_sc = c.get("/surgical-coordinator").get_data(as_text=True)
ck("a single-board page is titled by its board, without a doubled KPI",
   "<title>Surgical Coordinator KPI</title>" in _sc, "surgical <title> wrong")
ck("billing's single page reads Billing KPI Board",
   "<title>Billing KPI Board</title>" in c.get("/billing").get_data(as_text=True), "billing <title> wrong")

ck("the phone layout wraps the name cell instead of clipping pills",
   ".nm{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.3}" in html, "phone .nm still nowrap")
ck("the phone strip puts its caption on its own line", ".now .cap{grid-column:1 / -1;" in html, "phone caption not on its own line")
ck("the preset row scrolls instead of wrapping on phones", ".ctl{flex-wrap:nowrap;overflow-x:auto;" in html, "presets still wrap")

ck("the pills carry a disclaimer about lag and lunch, under the live strip",
   'class="disc">Status pills trail the phones by up to ~2 minutes.' in html and "not lunch, break, wrap-up or offline" in html,
   "disclaimer missing")

# ---- the lead distribution LINK (no frame) ----
_real_embed = A.DISTRIBUTION_BOARD_URL
A.DISTRIBUTION_BOARD_URL = ""
try:
    h0 = c.get("/v6").get_data(as_text=True)
    ck("no board URL -> no section on the page", 'var EMBED_URL = ""' in h0 and "lnkrow" in h0, "link markup gated on EMBED_URL")
    A.DISTRIBUTION_BOARD_URL = "https://example.test/board/x?k=SECRET"
    hx = c.get("/v6").get_data(as_text=True)
    ck("with a board URL the landing page carries it", 'var EMBED_URL = "https://example.test/board/x?k=SECRET"' in hx, "board url not passed")
    ck("the single-board pages do not", "example.test" not in c.get("/surgical-coordinator").get_data(as_text=True), "url leaked to a team page")
finally:
    A.DISTRIBUTION_BOARD_URL = _real_embed
ck("the section is titled Lead Distribution by default", A.DISTRIBUTION_BOARD_TITLE == "Lead Distribution"
   and 'var EMBED_TITLE = "Lead Distribution"' in html, A.DISTRIBUTION_BOARD_TITLE)
import glob as _glob
_me = os.path.basename(__file__)
_leak = [f for f in _glob.glob("*.py") + _glob.glob("templates/*.html")
         if f != _me and "backoffice-app" in open(f, encoding="utf-8").read()]
ck("the back-office share token is NOT in the repository (it is public)", _leak == [], _leak)
ck("the board is ONE link that opens a new tab -- no iframe, no preview card",
   '<p class="lnkrow"><a href="' in html and 'target="_blank" rel="noopener"' in html
   and "<iframe" not in html and 'class="pv"' not in html and "pointer-events:none" not in html, "frame remnants or link missing")

# ---- in production the first CRM read must not hold the request ----
import threading as _th
_gate = _th.Event(); _ran = _th.Event()
def _slow_bd(s_, e_, ids, start_date=None, end_date=None):
    _gate.wait(5); _ran.set(); return {}
A._zoho.list_users = lambda: {"u-judith": "Judith"}
A._zoho.activity_breakdown = _slow_bd
A._crm_bd_cache.clear(); A._crm_bd_inflight.clear(); A._CRM_ASYNC = True
try:
    _rep = {"ranked": [{"name": "Judith Merlo"}], "silent": [], "stalled": [], "unknown": []}
    _t0 = time.time(); A._attach_crm_created(_rep, "2026-07-29T00:00:00-04:00", "2026-07-29T23:59:59-04:00"); _took = time.time() - _t0
    ck("the request does not wait for a cold CRM read", _took < 1.0, round(_took, 2))
    ck("and says the read is in progress, not 0", _rep["ranked"][0]["crm"] is None and "being read" in (_rep.get("crm_meta") or {}).get("error", ""), _rep.get("crm_meta"))
    _gate.set()
    ck("the read does run, on a thread", _ran.wait(5), "breakdown never ran")
    # a read stuck in flight for longer than the guard is started again, not waited on forever
    A._crm_bd_cache.clear(); A._crm_bd_inflight.clear()
    _key = ("2026-07-29T00:00:00-04:00", "2026-07-29T23:59:59-04:00", ("Judith Merlo",))
    A._crm_bd_inflight[_key] = time.time() - (A._CRM_BD_STUCK + 5)
    _ran.clear(); _gate.set()
    _rep2 = {"ranked": [{"name": "Judith Merlo"}], "silent": [], "stalled": [], "unknown": []}
    A._attach_crm_created(_rep2, "2026-07-29T00:00:00-04:00", "2026-07-29T23:59:59-04:00")
    ck("a stuck in-flight read older than the guard is restarted", _ran.wait(5), "no new read started")
    ck("and /api/build shows the breakdown state", "crm_breakdown" in (c.get("/api/build").get_json() or {}), "diag missing")
    # when the read lands, the report cache for that window is dropped so the next poll carries the CRM
    A._v6_cache[("2026-07-29", "2026-07-29", -240, "surgical")] = {"at": time.time(), "report": {"meta": {}}, "ttl": 150.0}
    A._v6_cache[("2026-07-30", "2026-07-30", -240, "surgical")] = {"at": time.time(), "report": {"meta": {}}, "ttl": 150.0}
    A._CRM_ASYNC = False; A._crm_bd_cache.clear(); A._crm_bd_inflight.clear()
    A._zoho.activity_breakdown = lambda s_, e_, ids, start_date=None, end_date=None: {"u-judith": {"calls": {"created": 0, "due": 0, "completed": 0, "overdue": 0}, "tasks": {"created": 0, "due": 0, "completed": 0, "open": 0}}}
    A._attach_crm_created({"ranked": [{"name": "Judith Merlo"}], "silent": [], "stalled": [], "unknown": []}, "2026-07-29T00:00:00-04:00", "2026-07-29T23:59:59-04:00")
    ck("the report cache for that window is dropped when the CRM read lands; other windows stay",
       ("2026-07-29", "2026-07-29", -240, "surgical") not in A._v6_cache and ("2026-07-30", "2026-07-30", -240, "surgical") in A._v6_cache, sorted(A._v6_cache))
    A._v6_cache.pop(("2026-07-30", "2026-07-30", -240, "surgical"), None); A._CRM_ASYNC = True
finally:
    _gate.set()
    A._zoho.list_users, A._zoho.activity_breakdown = _real_users, _real_bd
    A._zoho.journey_breakdown = _real_jbd
    A._crm_bd_cache.clear(); A._crm_bd_inflight.clear()

# No tally and no exit here. Until 2026-10-13 this spot printed "0 failed" and
# called sys.exit, so the 150 lines of checks below it -- the pace column, the
# SMS chip, missed calls, the progress hairline -- never ran once while the
# suite read green. The one tally is at the END of the file.

# ---- Danny, 2026-10-07 evening: no pace column on surgical; the SMS chip is back on both boards ----
_tpl2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html"), encoding="utf-8").read()
ck("surgical reports pace_column off, billing on", A._TEAM_PACE_COLUMN.get("surgical") is False and A._TEAM_PACE_COLUMN.get("billing", True) is True, A._TEAM_PACE_COLUMN)
# Built behind the same platform stubs as the first build above: with the real
# fetchers back in place the report is a token error with no pace_column at all.
_sv2 = (A._v6_fetch_ringex, A._v6_cx_rows_for_team, A._v6_fetch_sms)
A._v6_fetch_ringex = lambda seats, d_, lt, tz: ({x["name"]: {"rows": [], "ext": x["ext"], "ext_id": x["ext_id"], "complete": True,
                                                            "missing_days": []} for x in seats}, {"cached": 0, "fetched": 0, "missing": 0})
A._v6_cx_rows_for_team = lambda t, d_, seats: ({x["name"]: [] for x in seats}, len(d_), {})
A._v6_fetch_sms = lambda r_, d_, lt, tz: ({}, {})
try:
    _cls.configured = property(lambda self: True)      # the wrapper's "not configured" gate, lifted
    _rj = c.get("/api/v6/report?team=surgical&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
    _rb = c.get("/api/v6/report?team=billing&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
finally:
    A._v6_fetch_ringex, A._v6_cx_rows_for_team, A._v6_fetch_sms = _sv2
    if _real_cfg is not None: _cls.configured = _real_cfg
ck("and the report JSON carries it", _rj.get("pace_column") is False and _rb.get("pace_column") is True,
   (_rj.get("pace_column"), _rb.get("pace_column"), str(_rj.get("error"))[:120], str(_rj.get("detail"))[:160]))
ck("the page drops the column and its heading where pace_column is false",
   "var paceCol = live && d.pace_column !== false;" in _tpl2 and "(paceCol ? '<div class=\"a-pc\">vs expected</div>' : '')" in _tpl2
   and "(paceCol ? paceCell(pace, 'talk_minutes', 'm') : '')" in _tpl2 and ".live.nopace .hd,.live.nopace .r{" in _tpl2 and ".live.nopace .r{grid-template-areas:\"nm tk tk\"" in _tpl2, "pace column not gated")
ck("the subtle SMS chip is back", "var SHOW_SMS_CHIP = true;" in _tpl2, "SMS chip still off")

# ---- phones: the no-pace grid keeps three columns; a live call is a dot; no Listen/Whisper ----
_ph = _tpl2.split("@media(max-width:560px)", 1)[1]
ck("the phone column rule names the no-pace variants (or the name sits in the 20px rank column)",
   ".live.nopace .r,.live.nopace.hascoll .r{\n      grid-template-columns:minmax(0,1fr) 60px 56px;" in _ph, "nopace rows keep desktop columns on phones")
ck("on phones Listen/Whisper are hidden and On a Call is a green dot",
   ".mon{display:none}" in _ph and ".pz.pz-on_call{font-size:0" in _ph and "border-radius:50%;background:#16a34a" in _ph, "phone dot/monitor rules missing")
ck("and the SMS arrows are NOT hidden on phones (every seat, both boards)", ".sms{display:none}" not in _ph and ".sms{font-size:10px" in _ph, "SMS chip hidden on phones")

# ---- Danny, 2026-10-08: how the day was worked (panel only) ----
def _r8(hhmm, dur, out=True, res=None, num="5551234567"):
    h, m = hhmm.split(":")
    return {"direction": "Outbound" if out else "Inbound", "result": res or ("Call connected" if out else "Accepted"),
            "duration": dur, "start_time": "2026-10-07T%02d:%02d:00-04:00" % (int(h), int(m)), "number": num}
_rows8 = [_r8("10:14", 300, num="1"), _r8("10:30", 60, res="No Answer", num="2"), _r8("10:40", 120, num="1"),
          _r8("12:05", 600, num="3"), _r8("13:50", 200, out=False, num="4"), _r8("16:40", 100, num="2")]
_sms8 = [{"direction": "Inbound", "start_time": "2026-10-07T14:00:00Z", "from_number": "+19998887777", "to_number": "+12125550100"},
         {"direction": "Inbound", "start_time": "2026-10-07T14:03:00Z", "from_number": "+19998887777", "to_number": "+12125550100"},
         {"direction": "Outbound", "start_time": "2026-10-07T14:12:00Z", "from_number": "+12125550100", "to_number": "+19998887777"},
         {"direction": "Inbound", "start_time": "2026-10-07T18:00:00Z", "from_number": "+17776665555", "to_number": "+12125550100"}]
_rep8 = build_report({"Judith Merlo": {"rows": _rows8, "ext": "185", "ext_id": 1, "complete": True,
                                       "shift": {"start": "10:00", "end": "19:00", "off": ["sat", "sun"]}}},
                     tz_offset_minutes=-240, window={"start": "2026-10-07", "end": "2026-10-07"},
                     sms_by_agent={"Judith Merlo": {"rows": _sms8, "complete": True, "missing_days": []}})
_a8 = _rep8["ranked"][0]; _f8 = _a8["day"]["focus"]
ck("started 14 min after a 10:00 shift", _f8["first"] == "10:14 AM" and _f8["late_minutes"] == 14, _f8)
ck("a finished day knows the last call and how early it was", _f8["last"] == "4:40 PM" and _f8["early_minutes"] == 140, _f8)
ck("the longest silence is ring-to-ring, after the previous call ended", _f8["gap_minutes"] == 167 and _f8["gap_window"].startswith("1:53 PM"), _f8)
ck("connect rate is answered dials over dials", _a8["reach"]["connect_rate_pct"] == 80.0 and _a8["reach"]["dials"] == 5, _a8["reach"])
ck("people reached counts numbers, not dials", _a8["reach"]["unique_outbound"] == 3 and _a8["reach"]["unique_contacts"] == 4 and _a8["reach"]["dials_per_contact"] == 1.7, _a8["reach"])
_rp8 = _a8["sms"]["replies"]
ck("two inbound texts before a reply are ONE wait; the reply took 12 min", _rp8["replied"] == 1 and _rp8["median_minutes"] == 12, _rp8)
ck("an inbound with no reply is unanswered", _rp8["unanswered"] == 1 and _rp8["threads"] == 2, _rp8)
# Danny, 2026-10-15: the page's tone needs to tell a NEW unanswered text from
# the ones already muted, so each one carries an opaque key -- last four
# digits and the minute it has waited since, never the number.
ck("...and carries one opaque key per unanswered thread",
   len(_rp8.get("unanswered_keys") or []) == 1 and _rp8["unanswered_keys"][0] == "5555@2026-10-07 14:00"
   and all(len(k.split("@")[0]) == 4 for k in _rp8["unanswered_keys"]), _rp8.get("unanswered_keys"))
_rep8b = build_report({"Judith Merlo": {"rows": _rows8, "ext": "185", "ext_id": 1, "complete": True}},
                      tz_offset_minutes=-240, window={"start": "2026-10-07", "end": "2026-10-07"})
ck("no shift on file -> no lateness judged, start still shown", _rep8b["ranked"][0]["day"]["shift_days"] == 0 and _rep8b["ranked"][0]["day"]["focus"]["late_minutes"] is None and _rep8b["ranked"][0]["day"]["focus"]["first"] == "10:14 AM", _rep8b["ranked"][0]["day"])
# The real converter, fed one delivered report: "Lead Phone" (ani) is the
# patient in BOTH directions, "Caller ID" (dnis) the company line. Checking
# the stubbed rows of the first build proved nothing about this.
_sv9 = (A._inbox_paths_all_scopes, A._parse_inbox_cached)
A._inbox_paths_all_scopes = lambda day: ["one-report"]
A._parse_inbox_cached = lambda path: [
    {"agent_name": "Judith Merlo", "direction": "Outbound", "result": "Outbound Answered", "ani": "3055550101", "dnis": "3055550000", "start_time": "10:00:00", "talk_time": 60},
    {"agent_name": "Judith Merlo", "direction": "Inbound", "result": "Inbound Missed", "ani": "3055550202", "dnis": "3055550000", "start_time": "10:05:00", "talk_time": 0}]
try:
    _cx9, _fd9, _ = A._v6_cx_rows_for_team("surgical", ["2026-10-13"], [{"name": "Judith Merlo", "ext": "185", "ext_id": 1}])
finally:
    A._inbox_paths_all_scopes, A._parse_inbox_cached = _sv9
_cx9r = _cx9.get("Judith Merlo") or []
ck("a RingCX row carries the other party's number both ways, never the company line",
   _fd9 == 1 and [r.get("number") for r in _cx9r] == ["3055550101", "3055550202"]
   and [r["direction"] for r in _cx9r] == ["Outbound", "Inbound"] and [r["result"] for r in _cx9r] == ["Call connected", "Missed"], _cx9r)
_tpl8 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html"), encoding="utf-8").read()
ck("the panel carries Day, Reach, text replies and the Journeys lines -- the row does not",
   "blk('Day', dayLines" in _tpl8 and "blk('Reach', reachLines" in _tpl8 and "'Text replies'" in _tpl8 and "blk('Surgery readiness', readyLines" in _tpl8
   and "'Planner calls'" in _tpl8 and "chip(so.labs, 'Labs pending'" in _tpl8 and "a.day" not in _tpl8.split("function presenceChip")[0].split("var SHOW_CRM_CHIP")[0], "panel lines missing or leaked to the row")
ck("every panel label has a definition behind it (tooltips), and the two-column grid exists",
   "var TIP = {" in _tpl8 and "title=\"' + esc(t) + '\"" in _tpl8 and "'Not ready':" in _tpl8 and "'Connect rate':" in _tpl8
   and "@media(min-width:760px){.lns{grid-template-columns:repeat(2,minmax(0,1fr))}}" in _tpl8, "tooltips or grid missing")
ck("nothing was due -> plain words, never '2 of 0 due done'", "' done', 'none due" in _tpl8, "zero-due wording missing")
ck("the long-call tip is set AFTER the TIP map exists (it threw 'Cannot set properties of undefined' before)",
   _tpl8.index("var TIP = {") < _tpl8.index("TIP[longLbl] = "), "TIP[longLbl] assigned before var TIP")

# ---- Danny, 2026-10-12: a KPI tracker per coordinator in priority order ----
ck("the work queue rides on the Journeys read", (jcrm or {}).get("journeys", {}).get("queue", {}).get("to_schedule") == 2
   and (jcrm or {}).get("journeys", {}).get("queue", {}).get("calls_overdue") == 9, (jcrm or {}).get("journeys", {}).get("queue"))
ck("the panel has a Priorities block in the order: to schedule, inbound, tasks, scheduled calls",
   "blk('Priorities', prioLines" in _tpl8 and _tpl8.index("'1 \\u00b7 To schedule'") < _tpl8.index("'2 \\u00b7 Inbound'") < _tpl8.index("'3 \\u00b7 Tasks'") < _tpl8.index("'4 \\u00b7 Scheduled calls'")
   and "(AGENT ? prioBlk + fixBlk : '')" in _tpl8 and "(AGENT ? '' : prioBlk + fixBlk)" in _tpl8, "priorities block missing or out of order")
ck("to-schedule links to Deals and names the oldest wait", "zl(ZOHO.deals, 'Deals')" in _tpl8 and "oldest waiting <b>" in _tpl8 and "deals: ZB + 'Potentials'" in _tpl8, "deals link missing")
ck("the collapsed row carries the queue line on every row builder, hidden on a coordinator's own page",
   _tpl8.count("smsChip(a) + srcChip(a) + queueChip(a)") == 3 and "function queueChip(a)" in _tpl8 and "if (!qu || AGENT) return '';" in _tpl8
   and ".agent .pq{display:none}" in _tpl8 and "' to schedule \\u00b7 '" in _tpl8, "queue chip missing")

# ---- billers don't schedule: no Journeys / Priorities for them, and no Journeys read on their behalf (Danny, 2026-10-12) ----
_jcalls = []
_real_users2, _real_bd2, _real_jbd2 = A._zoho.list_users, A._zoho.activity_breakdown, A._zoho.journey_breakdown
A._zoho.list_users = lambda: {"u-viv": "Vivian Martinez", "u-judith": "Judith"}
A._zoho.activity_breakdown = lambda s_, e_, ids, start_date=None, end_date=None: {u: {"calls": {"created": 0, "due": 0, "completed": 0, "overdue": 0}, "tasks": {"created": 0, "due": 0, "completed": 0, "open": 0}} for u in ids}
A._zoho.journey_breakdown = lambda ids, s_, e_, anchor: (_jcalls.append(list(ids)) or {u: {"active": 1, "soon": {}, "journey_calls": {}, "queue": {"to_schedule": 1}} for u in ids})
A._CRM_ASYNC = False; A._crm_bd_cache.clear(); A._crm_bd_inflight.clear()
try:
    _repb = {"ranked": [{"name": "Vivian Martinez"}, {"name": "Judith Merlo"}], "silent": [], "stalled": [], "unknown": []}
    A._attach_crm_created(_repb, "2026-07-29T00:00:00-04:00", "2026-07-29T23:59:59-04:00")
    _rows_b = {a["name"]: a for a in _repb["ranked"]}
    ck("billers get no Journeys block", (_rows_b["Vivian Martinez"].get("crm") or {}).get("journeys") is None, _rows_b["Vivian Martinez"].get("crm"))
    ck("coordinators still do", ((_rows_b["Judith Merlo"].get("crm") or {}).get("journeys") or {}).get("queue", {}).get("to_schedule") == 1, _rows_b["Judith Merlo"].get("crm"))
    ck("the Journeys read is made for coordinator ids only", _jcalls == [["u-judith"]], _jcalls)
finally:
    A._zoho.list_users, A._zoho.activity_breakdown, A._zoho.journey_breakdown = _real_users2, _real_bd2, _real_jbd2
    A._CRM_ASYNC = True; A._crm_bd_cache.clear(); A._crm_bd_inflight.clear()

# ---- Danny, 2026-10-13: missed calls with the callback number, cleared when the callback shows up ----
def _r13(hhmm, direction, result, num, name_rows):
    h, m = hhmm.split(":")
    name_rows.append({"direction": direction, "result": result, "duration": 60 if result in ("Call connected", "Accepted") else 0,
                      "start_time": "2026-10-13T%02d:%02d:00-04:00" % (int(h), int(m)), "number": num})
_ja, _oa = [], []
_r13("10:00", "Inbound", "Missed", "3055550101", _ja)          # Judith missed A ...
_r13("10:30", "Outbound", "No Answer", "3055550101", _ja)      # ... and called A back (no answer)
_r13("11:00", "Inbound", "Missed", "3055550202", _ja)          # missed B, nobody called
_r13("11:15", "Inbound", "Missed", "3055550303", _ja)          # missed C ...
_r13("11:40", "Outbound", "Call connected", "3055550303", _oa) # ... Oscar called C back
_r13("09:00", "Inbound", "Missed", "3055550404", _ja)          # missed D ...
_r13("09:20", "Inbound", "Accepted", "3055550404", _ja)        # ... D called again and was answered
_rep13 = build_report({"Judith Merlo": {"rows": _ja, "ext": "185", "ext_id": 1, "complete": True},
                       "Oscar Caballero": {"rows": _oa, "ext": "167", "ext_id": 2, "complete": True}},
                      tz_offset_minutes=-240, window={"start": "2026-10-13", "end": "2026-10-13"})
_j13 = next(a for a in _rep13["ranked"] + _rep13["silent"] + _rep13["unknown"] if a["name"] == "Judith Merlo")
_by13 = {m["digits"]: m for m in _j13["missed_calls"]}
ck("every missed inbound is listed with a formatted callback number", len(_by13) == 4 and _by13["3055550202"]["number"] == "(305) 555-0202", _j13["missed_calls"])
ck("a later outbound to the number marks it called back, with the time and the result", _by13["3055550101"]["resolved"] and _by13["3055550101"]["back"]["at"] == "10:30 AM" and _by13["3055550101"]["back"]["connected"] is False and _by13["3055550101"]["by_self"], _by13["3055550101"])
ck("a teammate's callback counts too, and says who", _by13["3055550303"]["resolved"] and _by13["3055550303"]["back"]["by"] == "Oscar Caballero" and not _by13["3055550303"]["by_self"], _by13["3055550303"])
ck("the number calling again and being answered clears it", _by13["3055550404"]["resolved"] and "answered" in _by13["3055550404"]["back"]["how"], _by13["3055550404"])
ck("one stays open", not _by13["3055550202"]["resolved"] and _j13["missed_summary"] == {"missed": 4, "called_back": 3, "open": 1}, _j13["missed_summary"])
ck("newest first", [m["at"] for m in _j13["missed_calls"]][0] == "11:15 AM", [m["at"] for m in _j13["missed_calls"]])
ck("reach counts people both ways: four numbers, one of them dialled", _j13["reach"]["unique_contacts"] == 4 and _j13["reach"]["unique_outbound"] == 1, _j13["reach"])
_t13 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html"), encoding="utf-8").read()
ck("the panel has a Missed calls block with tap-to-call numbers and a fix-list line",
   "blk('Missed calls', missedLines" in _t13 and "href=\"tel:+1' + esc(m.digits)" in _t13 and "line('Missed calls', ms.open" in _t13 and "'Missed calls':" in _t13, "missed calls block missing")
ck("the CX row carries Lead Phone as the number for both directions", '"number": r.get("ani") or ""' in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read(), "number mapping")

ck("the collapsed row carries a progress hairline whose length and colour follow calls+tasks done",
   _tpl8.count("queueChip(a) + progressBar(a)") == 3 and "function progressBar(a)" in _tpl8 and "var stops = [[176, 58, 49], [217, 119, 6], [22, 163, 74]];" in _tpl8
   and ".prog i{display:block;height:100%" in _tpl8 and "if (!due) return '';" in _tpl8, "progress bar missing")

# The planner's call subjects changed shape on 2026-10-13 ("Name · Attempt
# 1/4: check-in · Surg Nov 3"); matching only "Journey call -" counted zero
# planner calls and zero overdue for every coordinator, and the inbound seats'
# progress hairline had nothing to draw. Every Calls query in the Journeys read
# must accept both shapes.
import zoho_client as _zc
_seen_q = []
class _R204:
    status_code = 204; ok = True; text = ""
    def json(self): return {}
_real_post = _zc.requests.post
_zc.requests.post = lambda *a, **k: (_seen_q.append((k.get("json") or {}).get("select_query", "")) or _R204())
A._zoho._headers = lambda: {}          # no token refresh from a check
try:
    _zc_cls = type(A._zoho)
    _zc_cls.journey_breakdown(A._zoho, ["u-1"], "2026-10-13T00:00:00-04:00", "2026-10-13T23:59:59-04:00", "2026-10-13")
finally:
    _zc.requests.post = _real_post
    del A._zoho._headers
_callq = [q for q in _seen_q if " from Calls " in q and "Subject" in q]
ck("planner-call queries carry a subject filter", len(_callq) == 2, len(_callq))
ck("...that accepts the new subject shape", all("Subject like '% · Surg %'" in q for q in _callq), _callq)
ck("...and still the old prefix", all("Subject like 'Journey call -%'" in q for q in _callq), _callq)
ck("...as one OR'd pair, nested the way COQL accepts", all("and (Subject like 'Journey call -%' or Subject like '% · Surg %'))" in q for q in _callq), _callq)
ck("zero done still draws a red sliver", "var w = Math.max(pct, 4);" in _tpl8 and "width:' + w + '%" in _tpl8, "min width missing")

# ---- Danny, 2026-10-14: "add the shift started note to the rows" ----
# A seat whose shift began minutes before the report's cut reads as idle next
# to one that started at 10. The report says how far into the shift its data
# reaches, on ITS clock (the RingCX cut), and the row says so under two hours.
from datetime import datetime as _dt14
def _r14(hhmm, name_rows):
    h, m = hhmm.split(":")
    name_rows.append({"direction": "Outbound", "result": "Call connected", "duration": 60,
                      "start_time": "2026-10-14T%02d:%02d:00-04:00" % (int(h), int(m)), "number": "3055550199"})
_jm, _am, _jd = [], [], []
_r14("12:06", _jm); _r14("12:30", _jm); _r14("10:02", _jd)
_seats14 = {"Jorge Mier":   {"rows": _jm, "ext": "221", "ext_id": 1, "complete": True, "shift": {"start": "12:00", "end": "21:00", "off": ["sat", "sun"]}},
            "Alex Morales": {"rows": _am, "ext": "208", "ext_id": 2, "complete": True, "shift": {"start": "13:00", "end": "22:00", "off": ["sat", "sun"]}},
            "Judith Merlo": {"rows": _jd, "ext": "185", "ext_id": 3, "complete": True, "shift": {"start": "10:00", "end": "19:00", "off": ["sat", "sun"]}},
            "No Shift":     {"rows": [], "ext": "1", "ext_id": 4, "complete": True}}
_rep14 = build_report(_seats14, tz_offset_minutes=-240, window={"start": "2026-10-14", "end": "2026-10-14"},
                      now_local=_dt14(2026, 10, 14, 13, 18))      # the 1:18 PM report, a Wednesday
_by14 = {a["name"]: a for a in _rep14["ranked"] + _rep14["silent"] + _rep14["unknown"]}
ck("78 min into a 12:00 shift at the 1:18 PM report", _by14["Jorge Mier"]["day"]["shift_age_minutes"] == 78, _by14["Jorge Mier"]["day"])
ck("18 min into a 13:00 shift, even with no calls yet", _by14["Alex Morales"]["day"]["shift_age_minutes"] == 18, _by14["Alex Morales"]["day"])
ck("a 10:00 shift is 198 min old (the page hides it past 120)", _by14["Judith Merlo"]["day"]["shift_age_minutes"] == 198, _by14["Judith Merlo"]["day"])
ck("no shift on file -> no age", _by14["No Shift"]["day"]["shift_age_minutes"] is None, _by14["No Shift"]["day"])
_rep14b = build_report(_seats14, tz_offset_minutes=-240, window={"start": "2026-10-13", "end": "2026-10-13"},
                       now_local=_dt14(2026, 10, 14, 13, 18))
ck("a finished day carries no shift age", all(a["day"]["shift_age_minutes"] is None for a in _rep14b["ranked"] + _rep14b["silent"] + _rep14b["unknown"]), "age on a past window")
_rep14c = build_report({"Jorge Mier": _seats14["Jorge Mier"]}, tz_offset_minutes=-240, window={"start": "2026-10-14", "end": "2026-10-14"},
                       now_local=_dt14(2026, 10, 14, 11, 40))
ck("before the start the age is negative (the row says when the shift begins)", _by14 and _rep14c["ranked"][0]["day"]["shift_age_minutes"] == -20, _rep14c["ranked"][0]["day"])
_tpl14 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html"), encoding="utf-8").read()
# Danny, 2026-10-15: "remove shift started at on collapsed view" -- the note
# lives in the expanded day block only, and no chip function is left behind.
ck("the collapsed rows carry no shift note",
   "shiftChip" not in _tpl14 and ".sa{" not in _tpl14 and _tpl14.count("srcChip(a) + queueChip(a) + progressBar(a)") == 3, "shift chip still on the rows")
ck("the expanded day block says how far into the shift the report reaches, under two hours, live only",
   "dayLines += line('Into the shift', age + ' min'" in _tpl14 and "dayLines += line('Shift', 'starts ' + esc(hh12(dy.shift.start))" in _tpl14
   and "if ((LAST[CUR_SLOT] || {}).live && age != null && dy.shift && age < 120) {" in _tpl14, "panel line missing")
# ...and the coordinators' own pages get the day's progress in numbers, with
# the bar, at the top of Priorities (the queue line is hidden there).
ck("Priorities opens with the day's progress: done over due, planner calls and tasks, with the bar",
   "prioLines += line('Progress', doneP + ' of ' + dueP + ' done'" in _tpl14 and "progressBar(a) + (jcP.completed || 0) + ' of ' + (jcP.due || 0) + ' planner calls" in _tpl14
   and "pctP < 50 ? 'below' : ''" in _tpl14 and ".agent .prog{max-width:320px;height:5px}" in _tpl14, "progress line missing")

# Danny, 2026-10-15: Oscar's Yesterday read "99 of 48 done, 206% ... 63 of 2
# tasks". tasks.completed is every task CLOSED in the window; tasks.due is
# every task DUE in it. Done over due must take the same population on both
# sides: tasks due in the window that are no longer open.
_tpl15 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html"), encoding="utf-8").read()
ck("the hairline counts tasks done as due minus open, never closed-in-window",
   "var tdone = Math.max(0, (tt.due || 0) - (tt.open || 0));" in _tpl15 and "done = (jc.completed || 0) + tdone;" in _tpl15
   and "(tt.completed || 0) + ' of ' + (tt.due || 0) + ' tasks'" not in _tpl15, "hairline formula")
ck("...and so do the day strip and the panel's Progress line",
   "var mtdone = Math.max(0, (mtt.due || 0) - (mtt.open || 0));" in _tpl15 and "mdone = (mjc.completed || 0) + mtdone;" in _tpl15
   and "var tdoneP = Math.max(0, (ttP.due || 0) - (ttP.open || 0));" in _tpl15 and "doneP = (jcP.completed || 0) + tdoneP;" in _tpl15
   and "(mtt.completed || 0) + ' of ' + (mtt.due || 0)" not in _tpl15 and "(ttP.completed || 0) + ' of ' + (ttP.due || 0)" not in _tpl15, "strip/panel formula")
ck("the strip's percentage is capped at 100 and the queue shows on the live day only",
   "Math.min(100, Math.round(100 * mdone / mdue))" in _tpl15 and "(d.live ? (mq.calls_overdue ?" in _tpl15 and "(d.live && mq.to_schedule ?" in _tpl15, "cap or live gating")

# the tally stays LAST: on 2026-10-13 it sat mid-file and nine later checks ran uncounted
for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
