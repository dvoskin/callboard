"""Regression: "Follow Up Created" counts what a rep created in THIS window.

Created_Time, not Call_Start_Time. The question is what each rep logged or booked
during the shift being scored, whenever the call itself is scheduled for -- an
earlier version counted future-dated calls, which answered a different question
and counted a booking made last week against today.

Three ways it could quietly lie, one test each.

1. Owner on a Call is a SURNAME. This floor has three Rodriguezes -- Alexander,
   Grace and Francisco -- so surname attribution merges them. Ids are preferred;
   the surname is a fallback because this org's token lacks ZohoCRM.users.READ
   (production: 401 OAUTH_SCOPE_MISMATCH), and where a surname is shared the
   agents get None, not a number.

2. A partial fetch that looks complete. A non-OK page raises.

3. A total attribution failure must not render as a board of zeros -- that reads
   as "nobody created anything". It returns None, and the panel shows a dash.
"""
import os, sys, tempfile, types

sys.path.insert(0, __file__.rsplit('/', 1)[0])
os.environ.setdefault("INGEST_API_KEY", "k")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="data_"))
import app as A
import zoho_client as Z

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:220]) if not cond else ""))
    if not cond: fail.append(name)

BOARD = ["Alexander Rodriguez", "Grace Rodriguez", "Francisco Rodriguez",
         "Adelita Flowers", "Charlotte McKay"]
W = ("2026-08-21T00:00:00+00:00", "2026-08-21T23:59:59+00:00")


# ---- the CRM query itself -------------------------------------------------
def crm(rows, ok_status=200):
    def _post(url, headers=None, json=None, timeout=None):
        sent.append(json["select_query"])
        class R:
            status_code = ok_status
            ok = 200 <= ok_status < 300
            text = "server error"
            def json(self): return {"data": rows, "info": {"more_records": False}}
        return R()
    Z.requests = types.SimpleNamespace(post=_post, get=None)
    c = Z.ZohoClient.__new__(Z.ZohoClient); c.base_url = "https://x"; c._headers = lambda: {}
    return c

sent = []
c = crm([
    {"Owner": {"id": "1", "name": "Rodriguez"}},
    {"Owner": {"id": "1", "name": "Rodriguez"}},
    {"Owner": {"id": "4", "name": "Flowers"}},
    {"Owner": None},                      # malformed row must not crash
])
raw = c.count_activities_created(*W)
ok("queries on Created_Time, not Call_Start_Time",
   "Created_Time between" in sent[0] and "Call_Start_Time" not in sent[0], sent[0][:120])
ok("counts per owner id", raw["1"]["n"] == 2 and raw["4"]["n"] == 1, raw)
ok("carries the surname for the fallback", raw["4"]["name"] == "Flowers", raw)
ok("a malformed owner is skipped, not crashed", len(raw) == 2, raw)

sent = []
c = crm([], ok_status=500)
try:
    c.count_activities_created(*W)
    ok("a non-OK page raises", False, "returned a short count that looks complete")
except RuntimeError as e:
    ok("a non-OK page raises", "500" in str(e), str(e))


# ---- attribution ----------------------------------------------------------
RAW = {"1": {"name": "Rodriguez", "n": 4}, "2": {"name": "Rodriguez", "n": 6},
       "4": {"name": "Flowers", "n": 5}, "9": {"name": "Nobody", "n": 3}}

# ids available -> exact, and the Rodriguezes stay apart
A._zoho = types.SimpleNamespace(
    count_activities_created=lambda *a, **k: RAW,
    list_users=lambda: {"1": "Alexander Rodriguez", "2": "Grace Rodriguez",
                        "4": "Adelita Flowers"})
A._v5_crm_cache.clear()
by, meta = A._v5_activities_created(*W, BOARD)
ok("ids are preferred", meta.get("attribution") == "id", meta)
ok("Alexander gets 4", by.get("alexander rodriguez") == 4, by)
ok("Grace gets 6 -- not merged with Alexander", by.get("grace rodriguez") == 6, by)
ok("Adelita gets 5", by.get("adelita flowers") == 5, by)
ok("an unresolvable owner is unattributed", meta.get("unattributed") == 3, meta)
ok("an agent with none reads 0", by.get("charlotte mckay") == 0, by)

# no users scope -> surname fallback, shared surnames refuse to guess
A._zoho = types.SimpleNamespace(count_activities_created=lambda *a, **k: RAW,
                                list_users=lambda: {})
A._v5_crm_cache.clear()
by, meta = A._v5_activities_created(*W, BOARD)
ok("falls back to surname", meta.get("attribution") == "surname", meta)
ok("a unique surname attributes", by.get("adelita flowers") == 5, by)
ok("a shared surname yields None for every claimant",
   by.get("alexander rodriguez") is None and by.get("grace rodriguez") is None
   and by.get("francisco rodriguez") is None, by)
ok("the shared surname is named", meta.get("ambiguous_surnames") == ["rodriguez"], meta)
ok("shared and unknown are both unattributed", meta.get("unattributed") == 13, meta)

# nothing attributable at all -> None, never a board of zeros
A._zoho = types.SimpleNamespace(
    count_activities_created=lambda *a, **k: {"9": {"name": "Nobody", "n": 980}},
    list_users=lambda: {})
A._v5_crm_cache.clear()
by, meta = A._v5_activities_created(*W, BOARD)
ok("nothing attributed -> None", by is None, repr(by))
ok("and it says how many were lost", "980" in (meta.get("error") or ""), meta)

# a CRM failure -> None, not zeros
class Boom:
    def list_users(self): return {}
    def count_activities_created(self, *a, **k):
        raise RuntimeError("CRM COQL returned HTTP 401.")
A._zoho = Boom()
A._v5_crm_cache.clear()
by, meta = A._v5_activities_created(*W, BOARD)
ok("a CRM failure returns None", by is None, repr(by))
ok("the failure is reported", bool(meta.get("error")), meta)


# ---- list_users, exercised for real (it shipped with a NameError) ----------
def users_client(status, payload):
    def _get(url, headers=None, params=None, timeout=None):
        class R:
            status_code = status
            ok = 200 <= status < 300
            text = "server error"
            def json(self): return payload
        return R()
    Z.requests = types.SimpleNamespace(get=_get, post=None)
    c = Z.ZohoClient.__new__(Z.ZohoClient); c.base_url = "https://x"; c._headers = lambda: {}
    return c

u = users_client(200, {"users": [{"id": "1", "full_name": "Alexander Rodriguez"},
                                 {"id": "2", "first_name": "Grace", "last_name": "Rodriguez"},
                                 {"id": "3"}]}).list_users()
ok("list_users reads full_name", u.get("1") == "Alexander Rodriguez", u)
ok("list_users falls back to first+last", u.get("2") == "Grace Rodriguez", u)
ok("a nameless user is skipped", "3" not in u, u)
try:
    ok("a 401 returns {} without raising", users_client(401, {}).list_users() == {}, "raised")
except Exception as e:
    ok("a 401 returns {} without raising", False, "%s: %s" % (type(e).__name__, e))


# ---- the panel ------------------------------------------------------------
import io
tpl = io.open("templates/scoreboard_v5.html", encoding="utf-8").read()
ok("labelled Follow Up Created", "cell2('Follow Up Created'" in tpl)
ok("hidden at zero", "a.followups > 0 ? cell2('Follow Up Created'" in tpl)
ok("unknown still shows, as a dash", "a.followups == null ? cell2('Follow Up Created', '\u2013'" in tpl
   or "a.followups == null ? cell2('Follow Up Created', '–'" in tpl)
ok("the old label is gone", "Booked ahead" not in tpl)

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
