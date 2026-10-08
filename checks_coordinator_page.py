"""A coordinator's own page: /coordinator/<slug>?k=<token>.

Danny, 2026-10-08: "create for me a webpage where surgical coordinators can
view their own individual stats and performance separately". The link is
bound to one seat by an HMAC of its slug; the report and presence calls behind
the page are cut to that seat on the server; a coordinator token opens
nothing else (not the billing board, not another seat, not Listen/Whisper).
"""
import os
import sys
import json

os.environ.setdefault("V6_SMS_ENABLED", "0")
sys.argv = ["x"]
import app as A  # noqa: E402
from flask import jsonify  # noqa: E402

passed = failed = 0
def ck(label, ok, got=None):
    global passed, failed
    if ok: passed += 1
    else:
        failed += 1; print("  FAIL %s -- got %r" % (label, got))

c = A.app.test_client()
html_tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v6.html"), encoding="utf-8").read()
_real_secret, _real_gid = A.COORD_LINK_SECRET, A.GOOGLE_CLIENT_ID
_real_rep, _real_pres = A._api_v6_report_impl, A._api_v6_presence_impl

def fake_report():
    return jsonify({"team": "surgical", "live": True,
                    "ranked": [{"name": "Judith Merlo", "x": 1}, {"name": "Oscar Caballero", "x": 2}],
                    "silent": [{"name": "Alex Morales"}], "stalled": [], "unknown": [],
                    "warnings": [{"kind": "ranked_at_zero", "name": "Oscar Caballero"}, {"kind": "sms_incomplete"}],
                    "crm_meta": {"not_crm_users": ["Jorge Mier", "Judith Merlo"]}, "meta": {}})
def fake_presence():
    return jsonify({"team": "surgical", "seats": [{"name": "Judith Merlo", "state": "on_call"}, {"name": "Oscar Caballero", "state": "idle"}]})

try:
    # ---- no secret: everything about the links is a 404 / 503, never a leak
    A.COORD_LINK_SECRET = ""
    ck("no secret -> the page is a 404", c.get("/coordinator/judith-merlo?k=anything").status_code == 404)
    with c.session_transaction() as sess: sess["billing_pw"] = True
    r = c.get("/api/v6/coordinator-links")
    ck("no secret -> the links endpoint says what to set", r.status_code == 503 and "COORD_LINK_SECRET" in r.get_json().get("detail", ""), (r.status_code, r.get_json()))
    with c.session_transaction() as sess: sess.clear()

    # ---- with a secret
    A.COORD_LINK_SECRET = "test-secret-do-not-ship"
    slug = A._coord_slug("Judith Merlo"); tok = A._coord_token(slug)
    ck("the slug is the name, lowercase, dashed", slug == "judith-merlo", slug)
    ck("the token is 24 hex chars of an HMAC", len(tok) == 24 and all(ch in "0123456789abcdef" for ch in tok), tok)
    ck("a different secret gives a different token", A._coord_token(slug) != (A.COORD_LINK_SECRET.__class__("x") and __import__("hmac").new(b"other", slug.encode(), __import__("hashlib").sha256).hexdigest()[:24]))
    r = c.get("/coordinator/%s?k=%s" % (slug, tok)); h = r.get_data(as_text=True)
    ck("the right token opens the page", r.status_code == 200, r.status_code)
    ck("the page knows whose it is", 'var AGENT = "Judith Merlo"' in h and 'var AGENT_SLUG = "judith-merlo"' in h, "AGENT vars missing")
    ck("and is titled for them", "Judith Merlo" in h and "My Performance" in h, "title missing")
    ck("it is a share page on the surgical team", "var SHARE = true" in h and 'var FIXED = "surgical"' in h, "share/fixed missing")
    ck("a wrong token is a 404, not a 403", c.get("/coordinator/%s?k=%s" % (slug, "0" * 24)).status_code == 404)
    ck("a name not on the surgical roster is a 404 even with a well-formed token",
       c.get("/coordinator/ana-castro?k=%s" % A._coord_token("ana-castro")).status_code == 404)
    ck("a biller is not a coordinator", c.get("/coordinator/vivian-martinez?k=%s" % A._coord_token("vivian-martinez")).status_code == 404)

    # ---- the data behind the page: gate ON, one seat only
    A.GOOGLE_CLIENT_ID = "test-client-id"
    A._api_v6_report_impl = fake_report; A._api_v6_presence_impl = fake_presence
    r = c.get("/api/v6/report?team=surgical&agent=%s&k=%s" % (slug, tok)); j = r.get_json() or {}
    ck("the token unlocks the surgical report", r.status_code == 200, (r.status_code, j))
    ck("cut to the one seat: ranked", [a["name"] for a in j.get("ranked", [])] == ["Judith Merlo"], j.get("ranked"))
    ck("cut to the one seat: silent", j.get("silent") == [], j.get("silent"))
    ck("a warning naming someone else is dropped; a nameless one stays", j.get("warnings") == [{"kind": "sms_incomplete"}], j.get("warnings"))
    ck("other people's CRM gaps are not listed", (j.get("crm_meta") or {}).get("not_crm_users") == ["Judith Merlo"], j.get("crm_meta"))
    ck("the response says whose it is", j.get("agent_only") == "Judith Merlo", j.get("agent_only"))
    ck("without the token the report is refused", c.get("/api/v6/report?team=surgical&agent=%s" % slug).status_code == 401)
    ck("the token does NOT open the billing board", c.get("/api/v6/report?team=billing&agent=%s&k=%s" % (slug, tok)).status_code == 401)
    ck("the token does NOT open another seat", c.get("/api/v6/report?team=surgical&agent=oscar-caballero&k=%s" % tok).status_code == 401)
    ck("the token does NOT open the whole surgical board", c.get("/api/v6/report?team=surgical&k=%s" % tok).status_code == 401)
    r = c.get("/api/v6/presence?team=surgical&agent=%s&k=%s" % (slug, tok)); j = r.get_json() or {}
    ck("presence is cut to the one seat too", r.status_code == 200 and [x["name"] for x in j.get("seats", [])] == ["Judith Merlo"], (r.status_code, j))
    r = c.post("/api/v6/monitor?agent=%s&k=%s" % (slug, tok), json={"uii": "U", "destination": "7865551234", "verb": "listen"})
    ck("the token never reaches Listen/Whisper", r.status_code == 401, r.status_code)
    # a signed-in manager asking for one seat gets the same cut (a Google
    # session; the word password sets v5_pw/v7_pw -- billing_pw alone is the
    # old billing door and does not open the report)
    with c.session_transaction() as sess: sess["user"] = {"email": "danny@example.test"}
    r = c.get("/api/v6/report?team=surgical&agent=%s" % slug); j = r.get_json() or {}
    ck("a signed-in viewer can ask for one seat and gets only that seat", r.status_code == 200 and [a["name"] for a in j.get("ranked", [])] == ["Judith Merlo"], (r.status_code, j.get("ranked")))
    ck("an unknown agent is a 404 for them", c.get("/api/v6/report?team=surgical&agent=nobody-here").status_code == 404)
    r = c.get("/api/v6/coordinator-links"); j = r.get_json() or {}
    ck("a signed-in viewer gets every coordinator's link", r.status_code == 200 and len(j.get("links", [])) == 6, (r.status_code, len(j.get("links", []))))
    ck("each link carries its own slug and token", all(l["url"].endswith("/coordinator/%s?k=%s" % (l["slug"], A._coord_token(l["slug"]))) for l in j.get("links", [])), j.get("links"))
    ck("Ana Castro is not among them", all(l["name"] != "Ana Castro" for l in j.get("links", [])), [l["name"] for l in j.get("links", [])])
    with c.session_transaction() as sess: sess.clear()
    ck("the links are not handed to a share token or a coordinator token",
       c.get("/api/v6/coordinator-links?agent=%s&k=%s" % (slug, tok)).status_code == 401)
finally:
    A.COORD_LINK_SECRET, A.GOOGLE_CLIENT_ID = _real_secret, _real_gid
    A._api_v6_report_impl, A._api_v6_presence_impl = _real_rep, _real_pres

# ---- the page's own behaviour, in the template
ck("the page filters to its seat, hides the team strip and opens the panel",
   "if (AGENT) {   // belt and braces" in html_tpl and "if (d.live && !AGENT) {" in html_tpl
   and "mpanel.classList.add('on'); mine.setAttribute('aria-expanded', 'true');" in html_tpl, "agent mode missing")
ck("both fetches carry the seat", html_tpl.count("if (AGENT_SLUG) q.set('agent', AGENT_SLUG);") == 2, html_tpl.count("q.set('agent', AGENT_SLUG)"))
ck("the title is not overwritten by the team label", "if (d.team_label && !MULTI_BOARD && !AGENT) {" in html_tpl, "title override")

_sv = A.COORD_LINK_SECRET
try:
    A.COORD_LINK_SECRET = ""
    ck("/api/build says the links are NOT configured without the secret", c.get("/api/build").get_json().get("coordinator_links_configured") is False)
    A.COORD_LINK_SECRET = "x"
    ck("and that they are, with it (never the secret itself)", c.get("/api/build").get_json().get("coordinator_links_configured") is True and "x" != c.get("/api/build").get_json().get("coordinator_links_configured"))
finally:
    A.COORD_LINK_SECRET = _sv

ck("every tip says how to fix it or where to look",
   all(("To fix:" in html_tpl.split(k, 1)[1].split("\n", 1)[0]) or ("Where:" in html_tpl.split(k, 1)[1].split("\n", 1)[0])
       for k in ["'Started':", "'Longest gap':", "'Connect rate':", "'CRM calls':", "'Planner calls':", "'Not ready':", "'Text replies':"]), "a tip lacks its fix")
ck("the readiness chips link to the Zoho views (labs/balance -> Not Ready, clearance -> Medical Action Needed)",
   "chip(so.mc, 'Clearance pending'" in html_tpl and ", ZOHO.medical)" in html_tpl and html_tpl.count(", ZOHO.notReady)") == 2
   and "custom-view/5212466001043166677/list" in html_tpl and "custom-view/5212466001043166701/list" in html_tpl, "chip links missing")
ck("a coordinator's page opens with What to fix now, linked, and says All clear when nothing is red",
   "blk('What to fix now', fixLines" in html_tpl and "zl(ZOHO.myCalls, 'My Journey Calls')" in html_tpl
   and "line('All clear'" in html_tpl and "if (AGENT) {\n        var zl" in html_tpl, "fix list missing or not agent-only")

ck("a coordinator's page carries plain-language data notes with the report's own time and lag",
   "<div class=\"agentnotes\"><div class=\"grp\"" in html_tpl and "lagV = d.data_as_of.lag_minutes + ' min behind'" in html_tpl and "line('Late start', fo.late_minutes + ' min'" in html_tpl
   and "emailed every ~30 min" in html_tpl and "nl('On track for', 'a projection'" in html_tpl and "(AGENT ? fixBlk : '')" in html_tpl and html_tpl.index("if (AGENT) {\n      var nl = function") < html_tpl.index("if (d.live && !AGENT) {"), "agent notes missing")

print("%d passed" % passed)
print("%d failed" % failed)
