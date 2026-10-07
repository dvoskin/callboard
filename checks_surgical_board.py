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
# Danny, 2026-10-07 evening: SMS is back ON for surgical ("show inbound
# outbound sms for the surgical coordinators as well, sending from ringex").
ck("the surgical build reads SMS for its roster", _sms_calls == [13], _sms_calls)

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
    _rc = A._ringcx.active_calls_with_status
    A._ringcx.active_calls_with_status = lambda: ([], {"ok": False, "note": "no RingCX creds here", "read_at": 0, "http_error": None})
    A._cx_active_cache.update(at=0.0, calls=[], meta=None)
    try:
        j = c.get("/api/v6/presence?team=surgical").get_json()
    finally:
        A._ringcx.active_calls_with_status = _rc
        A._cx_active_cache.update(at=0.0, calls=[], meta=None)
    ck("every surgical seat is reported, from RingCX, not withheld as a team",
       len(j.get("seats", [])) == 13 and all(x.get("source") == "ringcx" for x in j["seats"]), j)
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
ck("a single-board page is still titled by its board", "if (d.team_label && !MULTI_BOARD) {" in html, "single-board title lost")
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
    ck("the surgical report says SMS is on", jj.get("sms_enabled") is True, jj.get("sms_enabled"))
    ck("sales stays off", A._team_sms_enabled("sales") is False, A._team_sms_enabled("sales"))
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
A._zoho.list_users = lambda: {"u-judith": "Judith Merlo", "u-oscar": "Oscar Caballero"}
def _stub_bd(s_, e_, ids, start_date=None, end_date=None):
    _seen_window.update(start=s_, end=e_, ids=sorted(ids), sd=start_date, ed=end_date)
    return {"u-judith": {"calls": {"created": 3, "due": 5, "completed": 4, "overdue": 1},
                         "tasks": {"created": 2, "due": 6, "completed": 5, "open": 1}}}
A._zoho.activity_breakdown = _stub_bd
A._crm_bd_cache.clear()
A._v6_cache.clear()          # the earlier request cached rows without CRM on them
try:
    _cls.configured = property(lambda self: True)
    A._v6_fetch_sms = lambda r_, d_, lt, tz: ({}, {})
    jc = c.get("/api/v6/report?team=surgical&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
    jb2 = c.get("/api/v6/report?team=billing&start=2026-07-29&end=2026-07-29&tz=240").get_json() or {}
finally:
    A._zoho.list_users, A._zoho.activity_breakdown = _real_users, _real_bd
    A._crm_bd_cache.clear()
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
ck("a CRM user has the full breakdown on her row",
   jcrm == {"calls": {"created": 3, "due": 5, "completed": 4, "overdue": 1},
            "tasks": {"created": 2, "due": 6, "completed": 5, "open": 1}}, jcrm)
ck("a CRM user with nothing in the window has real zeros",
   rows_c.get("Oscar Caballero", {}).get("crm", {}).get("calls", {}).get("completed") == 0, rows_c.get("Oscar Caballero", {}).get("crm"))
ck("someone who is not a CRM user by name is None, not 0", "crm" in rows_c.get("Jorge Mier", {}) and rows_c["Jorge Mier"]["crm"] is None,
   rows_c.get("Jorge Mier", {}).get("crm", "missing"))
ck("and is named in the meta so the gap is visible", "Jorge Mier" in (jc.get("crm_meta") or {}).get("not_crm_users", []),
   jc.get("crm_meta"))
ck("only the board's CRM ids are queried", _seen_window.get("ids") == ["u-judith", "u-oscar"], _seen_window.get("ids"))
ck("tasks due uses DATE bounds, calls use datetimes",
   _seen_window.get("sd") == "2026-07-29" and _seen_window.get("ed") == "2026-07-29", _seen_window)
ck("the row chip (when shown) reads completed counts", "CRM <b>' + c.completed + '</b> calls" in html, "chip not on completed counts")
ck("but the chip is hidden in the collapsed row for now", "var SHOW_CRM_CHIP = false;" in html and "if (!SHOW_CRM_CHIP || !D.crm_enabled) return '';" in html,
   "CRM chip not gated off")
ck("the panel's CRM lines are present but switched off for now",
   "var SHOW_CRM_PANEL = false;" in html and "line('CRM calls'" in html, "CRM panel lines missing or not gated")
ck("billing does not carry CRM", jb2.get("crm_enabled") is False and "crm_created" not in (jb2.get("ranked") or [{}])[0],
   (jb2.get("crm_enabled"), sorted((jb2.get("ranked") or [{}])[0])))
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

ck("the SMS chip is off the collapsed row for now (panel line stays)",
   "var SHOW_SMS_CHIP = false;" in html and "if (!SHOW_SMS_CHIP) return '';" in html and "smsLine = line('SMS'" in html,
   "SMS chip not gated / panel line missing")
ck("the live strip exists and agrees with the table's quiet count",
   "class=\"now\"" in html and "nQuiet = quietSeats.length" in html, "live strip missing")
ck("the panel is grouped", "grp('Quality')" in html and "grp('Schedule &amp; line')" in html, "panel not grouped")

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

# ---- the framed distribution board ----
_real_embed = A.DISTRIBUTION_BOARD_URL
A.DISTRIBUTION_BOARD_URL = ""
try:
    ck("no embed URL -> no frame on the page", 'class="embed"' in c.get("/v6").get_data(as_text=True)
       and "var EMBED_URL = \"\"" in c.get("/v6").get_data(as_text=True), "frame markup gated on EMBED_URL")
    A.DISTRIBUTION_BOARD_URL = "https://example.test/board/x?k=SECRET"
    hx = c.get("/v6").get_data(as_text=True)
    ck("with an embed URL the landing page carries it", 'var EMBED_URL = "https://example.test/board/x?k=SECRET"' in hx, "embed url not passed")
    ck("the single-board pages do not", "example.test" not in c.get("/surgical-coordinator").get_data(as_text=True), "embed leaked to a team page")
finally:
    A.DISTRIBUTION_BOARD_URL = _real_embed
import glob as _glob
_me = os.path.basename(__file__)
_leak = [f for f in _glob.glob("*.py") + _glob.glob("templates/*.html")
         if f != _me and "backoffice-app" in open(f, encoding="utf-8").read()]
ck("the back-office share token is NOT in the repository (it is public)", _leak == [], _leak)

print("%d passed" % passed)
for e in errors:
    print("  FAIL", e)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
