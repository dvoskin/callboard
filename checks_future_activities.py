"""Regression: "booked ahead" counts bookings, by identity, and never guesses.

Three ways this metric could quietly lie, one test each.

1. Counting every future Call would re-count the dialling that talk time already
   measures. A booking is a Call with NO disposition -- this org's own convention,
   already used by get_scheduled_followup_calls.

2. Owner on a Call is a SURNAME. This floor has three Rodriguezes -- Alexander,
   Grace and Francisco -- so surname attribution silently merges them. It must go
   through the owner id.

3. A CRM failure must not read as "booked nothing". It returns None, and the panel
   shows a dash.
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

# Three Rodriguezes, all surname "Rodriguez", plus a booked-and-worked pair.
USERS = {"1": "Alexander Rodriguez", "2": "Grace Rodriguez",
         "3": "Francisco Rodriguez", "4": "Adelita Flowers"}
ROWS = [
    {"Owner": {"id": "1", "name": "Rodriguez"}, "Outgoing_call_disposition": None},
    {"Owner": {"id": "1", "name": "Rodriguez"}, "Outgoing_call_disposition": ""},
    {"Owner": {"id": "2", "name": "Rodriguez"}, "Outgoing_call_disposition": None},
    {"Owner": {"id": "3", "name": "Rodriguez"}, "Outgoing_call_disposition": None},
    {"Owner": {"id": "3", "name": "Rodriguez"}, "Outgoing_call_disposition": None},
    {"Owner": {"id": "3", "name": "Rodriguez"}, "Outgoing_call_disposition": None},
    # already worked -- a logged call, not a booking
    {"Owner": {"id": "4", "name": "Flowers"}, "Outgoing_call_disposition": "Connected"},
    {"Owner": {"id": "4", "name": "Flowers"}, "Outgoing_call_disposition": "Left Voicemail"},
    {"Owner": {"id": "4", "name": "Flowers"}, "Outgoing_call_disposition": None},
]

def stub(rows, pages=1):
    calls = {"n": 0}
    def _post(url, headers=None, json=None, timeout=None):
        i = calls["n"]; calls["n"] += 1
        class R:
            status_code, ok = 200, True
            text = ""
            def json(self):
                return {"data": rows if i == 0 else [],
                        "info": {"more_records": i < pages - 1}}
        return R()
    Z.requests = types.SimpleNamespace(post=_post, get=None)
    c = Z.ZohoClient.__new__(Z.ZohoClient)
    c.base_url = "https://x"; c._headers = lambda: {}
    return c, calls

# 1. only undispositioned calls count
c, _ = stub(ROWS)
counts = c.count_future_activities("a", "b")
ok("a worked call is not a booking", counts.get("4") == 1, counts)
ok("undispositioned calls are counted", counts.get("3") == 3, counts)
ok("an empty-string disposition still counts as unworked", counts.get("1") == 2, counts)

# 2. the three Rodriguezes stay apart
ok("owner ids are kept distinct",
   counts.get("1") == 2 and counts.get("2") == 1 and counts.get("3") == 3, counts)
A._zoho = types.SimpleNamespace(list_users=lambda: USERS,
                                count_future_activities=lambda *a, **k: counts)
A._v5_crm_cache.clear()
by_agent, meta = A._v5_future_activities()
ok("Alexander Rodriguez gets 2", by_agent.get("alexander rodriguez") == 2, by_agent)
ok("Grace Rodriguez gets 1", by_agent.get("grace rodriguez") == 1, by_agent)
ok("Francisco Rodriguez gets 3", by_agent.get("francisco rodriguez") == 3, by_agent)
ok("surnames were not merged", len([k for k in by_agent if "rodriguez" in k]) == 3, by_agent)

# 3. an unknown owner id is reported, not silently dropped into someone else
A._zoho = types.SimpleNamespace(list_users=lambda: {"1": "Alexander Rodriguez"},
                                count_future_activities=lambda *a, **k: {"1": 2, "99": 7})
A._v5_crm_cache.clear()
by_agent, meta = A._v5_future_activities()
ok("an unresolvable owner is counted as unattributed", meta.get("unattributed") == 7, meta)
ok("it is not attributed to anyone", sum(by_agent.values()) == 2, by_agent)

# 4. a CRM failure yields None, never a confident zero
class Boom:
    def list_users(self): return {}
    def count_future_activities(self, *a, **k):
        raise RuntimeError("CRM COQL returned HTTP 401.")
A._zoho = Boom()
A._v5_crm_cache.clear()
by_agent, meta = A._v5_future_activities()
ok("a CRM failure returns None", by_agent is None, repr(by_agent))
ok("an empty dict would have read as 'booked nothing'", by_agent != {}, repr(by_agent))
ok("the failure is reported", bool(meta.get("error")), meta)

# 5. a partial fetch must raise rather than under-report
def _bad_post(url, headers=None, json=None, timeout=None):
    class R:
        status_code, ok = 500, False
        text = "boom"
        def json(self): return {}
    return R()
Z.requests = types.SimpleNamespace(post=_bad_post, get=None)
c2 = Z.ZohoClient.__new__(Z.ZohoClient); c2.base_url = "https://x"; c2._headers = lambda: {}
try:
    c2.count_future_activities("a", "b")
    ok("a non-OK page raises", False, "returned a short count that looks complete")
except RuntimeError as e:
    ok("a non-OK page raises", "500" in str(e), str(e))

# 6. the template must not print 0 when the number is unknown
import io
tpl = io.open("templates/scoreboard_v5.html", encoding="utf-8").read()
ok("panel shows a dash when unknown", "a.followups == null ? '–'" in tpl)
ok("panel has the Booked ahead cell", "cell2('Booked ahead'" in tpl)

# 7. exercise the REAL list_users, including its failure paths.
# The first version of this file stubbed _zoho entirely, so list_users never ran
# and shipped with `log.warning` against a name that does not exist in
# zoho_client -- the module has no global `log`, every other method makes one
# locally. Production said: error "name 'log' is not defined", booked ahead "–".
# A stub that replaces the unit under test proves nothing about it.
def users_client(status, payload):
    def _get(url, headers=None, params=None, timeout=None):
        class R:
            status_code = status
            ok = 200 <= status < 300
            text = "server error"
            def json(self): return payload
        return R()
    Z.requests = types.SimpleNamespace(get=_get, post=None)
    c = Z.ZohoClient.__new__(Z.ZohoClient)
    c.base_url = "https://x"; c._headers = lambda: {}
    return c

c = users_client(200, {"users": [
    {"id": "1", "full_name": "Alexander Rodriguez"},
    {"id": "2", "first_name": "Grace", "last_name": "Rodriguez"},
    {"id": "3"},                                   # no name at all
]})
u = c.list_users()
ok("list_users reads full_name", u.get("1") == "Alexander Rodriguez", u)
ok("list_users falls back to first+last", u.get("2") == "Grace Rodriguez", u)
ok("a nameless user is skipped", "3" not in u, u)

# the path that actually broke: a non-OK response logs and returns {}
try:
    c = users_client(500, {})
    ok("a non-OK users response returns {} without raising", c.list_users() == {}, "raised")
except Exception as e:
    ok("a non-OK users response returns {} without raising", False, "%s: %s" % (type(e).__name__, e))

# and an outright transport failure
def _boom(*a, **k): raise RuntimeError("connection reset")
Z.requests = types.SimpleNamespace(get=_boom, post=None)
c = Z.ZohoClient.__new__(Z.ZohoClient); c.base_url = "https://x"; c._headers = lambda: {}
try:
    ok("a transport failure returns {} without raising", c.list_users() == {}, "raised")
except Exception as e:
    ok("a transport failure returns {} without raising", False, "%s: %s" % (type(e).__name__, e))

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
