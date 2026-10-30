"""The Surgical Coordinator Trainees (Danny, 2026-10-26: "Laura, Victor, Keily and
Celine will be starting on RingCX today doing inbound calls for now"): their own
roster, board, /teams section, STOP scan, and an accent-proof RingCX name match.
Run: .venv/bin/python checks_trainees.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("COORD_LINK_SECRET", "teams-key-for-checks")   # the /teams key exists only with a secret
import app as A  # noqa: E402
passed, errors = 0, []
def ck(label, cond, got=None):
    global passed
    if cond: passed += 1
    else: errors.append("%s -- got %r" % (label, got))

r, meta = A._billing_roster("trainees")
names = [s["name"] for s in r]
ck("the trainees, in the order Danny named them (Gregory and Wellington joined 2026-10-30)",
   names == ["Laura Martinez", "Victor Silvestre", "Keily Fermín", "Celine Vasquez", "Gregory Beltran", "Wellington Santiago"], names)
for who in ("Gregory Beltran", "Wellington Santiago"):
    ck("%s is off the sales roster" % who, who not in [s["name"] for s in A._billing_roster("sales")[0]])
    ck("...and kept off the sales floor by being on another board, not as departed (%s)" % who,
       A._norm_name(who) in A._other_team_names() and A._norm_name(who) not in A._DEPARTED_SALES_NAMES)

# Their closings stay on the SALES total (Danny: "total closing should be 7 not 6").
books = {"gregory beltran": {"display": "Gregory Beltran", "quotes_sent": 2, "quotes_invoiced": 1, "retainers_sent": 1,
                             "retainers_paid": 1, "paid_amount": 500.0},
         "wellington santiago": {"display": "Wellington Santiago", "quotes_sent": 0, "quotes_invoiced": 0,
                                 "retainers_sent": 0, "retainers_paid": 0, "paid_amount": 0.0},
         "maria carvajal": {"display": "Maria Carvajal", "quotes_sent": 3, "quotes_invoiced": 0, "retainers_sent": 1,
                            "retainers_paid": 6, "paid_amount": 3000.0}}
rep = {"ranked": [{"name": "Maria Carvajal", "books": {"retainers_paid": 6}}], "unranked": []}
seen = {"maria carvajal"}
added = A._v5_moved_rows(rep, books, seen)
rows = rep["ranked"] + rep["unranked"]
ck("a moved rep with a closing keeps a sales row", added == ["Gregory Beltran"], added)
ck("...labelled with where he went", any(r["name"] == "Gregory Beltran (now a coordinator trainee)" for r in rep["unranked"]),
   [r["name"] for r in rep["unranked"]])
ck("...Books only: no calls", all(r.get("attempts") == 0 and r.get("talk") == 0 for r in rep["unranked"]))
ck("the sales total counts his closing (6 + 1 = 7)", sum((r.get("books") or {}).get("retainers_paid", 0) for r in rows) == 7)
ck("a moved rep with no Books activity gets no row", not any("Wellington" in r["name"] for r in rows))
ck("never twice", A._v5_moved_rows(rep, books, seen) == [])
ck("every seat is a RingCX seat with its RingEX extension (SMS, presence) and always_rank",
   all(s.get("source") == "ringcx" and s.get("ext_id") and s.get("ext") and s.get("always_rank") for s in r), r)
ck("RingEX ids are the ones the RingCX admin API gave (rcUserId)",
   {s["ext_id"] for s in r} == {1253951035, 1253948035, 1253942035, 1253947035, 1180435035, 492651034}, {s["ext_id"] for s in r})
ck("the team reads RingCX", A._TEAM_SOURCES.get("trainees") == "ringcx", A._TEAM_SOURCES.get("trainees"))
ck("it has Danny's name", A.TEAM_LABELS.get("trainees") == "Surgical Coordinator Trainees", A.TEAM_LABELS.get("trainees"))
ck("judged against customer service's bar (inbound calls), with no pace column",
   A.TEAM_TARGETS["trainees"] is A.TEAM_TARGETS["inbound"] and A.TEAM_PACE_CURVES["trainees"] is A.TEAM_PACE_CURVES["inbound"]
   and A._TEAM_PACE_COLUMN.get("trainees") is False, (A._TEAM_PACE_COLUMN,))
ck("not folded into the Surgical Coordinator table", "trainees" not in A._COMBINED_TEAMS["surgical"]["parts"], A._COMBINED_TEAMS["surgical"]["parts"])
ck("its own page and hub tile", A._TEAM_PATHS.get("trainees") == "coordinator-trainees"
   and any(rule.rule == "/coordinator-trainees" for rule in A.app.url_map.iter_rules())
   and any(t["key"] == "trainees" for t in A._hub_team_tiles()), A._TEAM_PATHS)
ck("the STOP scan reads their lines", "trainees" in A._OPTOUT_TEAMS, A._OPTOUT_TEAMS)
ck("the CRM block is on for the trainees (Danny: 'They are all CRM users too')", A._TEAM_CRM.get("trainees") is True, A._TEAM_CRM)
ck("Celine is matched to Zoho's spelling of her name", A._CRM_NAME_ALIASES.get("Celine Vasquez") == "Celine Dios Vasquez", A._CRM_NAME_ALIASES.get("Celine Vasquez"))
ck("the RingCX name match folds accents and spacing: a report's 'Keily  Fermin' is the directory's 'Keily Fermín'",
   A._cx_fold("Keily  Fermin") == A._cx_fold("Keily Fermín") == "keily fermin" and A._cx_fold(None) == "", A._cx_fold("Keily Fermín"))
A.app.config["TESTING"] = True
c = A.app.test_client()
_real = A._v6_allowed; A._v6_allowed = lambda: True
try:
    html = c.get("/teams").get_data(as_text=True)
finally:
    A._v6_allowed = _real
ck("/teams carries the section, after the coordinators and before billing",
   html.find('id="t-surgical"') < html.find('id="t-trainees"') < html.find('id="t-billing"') and "team=trainees" in html, html.find('id="t-trainees"'))
import re
v6_sections = re.findall(r"/api/v6/report\?team=([a-z]+)&", html)
ck("every v6 board the teams page shows is one the page's own key opens (trainees read 'unauthorized' on 2026-10-26)",
   v6_sections and all(t in A._TEAMS_KEY_BOARDS for t in v6_sections), (v6_sections, A._TEAMS_KEY_BOARDS))
_real6, _realword = A._v6_allowed, A._word_authed
A._v6_allowed = lambda: False; A._word_authed = lambda: False
_saved_impl = A._api_v6_report_impl
A._api_v6_report_impl = lambda: A.jsonify({"ranked": [], "meta": {}})
try:
    r = c.get("/api/v6/report?team=trainees&k=" + A._teams_token())
    ck("the teams key opens the trainee board", r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:80]))
    r = c.get("/api/v6/report?team=inbound&k=" + A._teams_token())
    ck("...and still not a board the page does not show", r.status_code == 401, r.status_code)
finally:
    A._v6_allowed, A._word_authed, A._api_v6_report_impl = _real6, _realword, _saved_impl
for e in errors: print("  FAIL " + e)
print("%d passed, %d failed" % (passed, len(errors)))
sys.exit(1 if errors else 0)
