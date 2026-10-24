"""The Shared Inbox on /teams (Danny, 2026-10-24: "add the shared inbox stats
for Sally Ariel Artem Uma Anna's rings for unopened and unanswered messages").

What must hold:
  * /api/teams/inbox reads the SAME cached build as /api/v5/sms -- the message
    store's budget is spent once -- and lists every shared line, Anna included.
  * Anna is teams_only: she stays OFF the sales board's panel (/api/v5/sms),
    which Danny set himself on 2026-10-15.
  * a line the store refused is "unknown": left out of the totals and counted
    apart, never shown as 0.
  * a window the build has not read yet says loading, not an empty inbox.
  * the endpoint opens on the /teams key and refuses without any gate.
Run: .venv/bin/python checks_teams_inbox.py
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("COORD_LINK_SECRET", "teams-key-for-checks")

import app as A  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


A.app.config["TESTING"] = True
c = A.app.test_client()
_cls = type(A._ringcx)
_real_cfg = _cls.__dict__.get("configured")
_cls.configured = property(lambda self: True)

anna = next((p for p in A._SALES_SMS_PANEL if p["label"] == "Anna"), None)
ck("Anna is on the shared-lines panel", bool(anna), [p["label"] for p in A._SALES_SMS_PANEL])
ck("...on her own line (ext 733, the Journeys sender)", anna and anna.get("ext") == "733" and anna.get("ext_id") == 683781035, anna)
ck("...and marked teams_only, so /v5 keeps Danny's 10-15 panel", anna and anna.get("teams_only") is True, anna)
ck("the other four lines are unchanged", [p["label"] for p in A._SALES_SMS_PANEL][:4] == ["Ariel", "Salome", "Artem", "Umed"],
   [p["label"] for p in A._SALES_SMS_PANEL])

# A finished build in the cache for one window; nothing is read from RingEX.
tz = 240
today = A.datetime.now(A.timezone.utc) - A.timedelta(minutes=tz)
day = today.date().isoformat()
key = (day, day, tz)


def line(label, teams_only=False, **kw):
    d = {"sent": 3, "received": 5, "unread": 1, "unanswered": 2, "sent_typed": 3, "sent_auto": 0,
         "complete": True, "unknown": False, "note": None, "label": label, "name": label + " X", "ext": "1",
         "teams_only": teams_only}
    d.update(kw)
    return d


panel = [line("Ariel"), line("Salome", unread=0, unanswered=0), line("Artem"),
         line("Umed", unknown=True, complete=False, note="429", sent=0, received=0, unread=0, unanswered=0),
         line("Anna", teams_only=True, unread=4, unanswered=1)]
with A._V5_SMS_LOCK:
    A._V5_SMS_CACHE.clear()
    A._V5_SMS_CACHE[key] = {"at": time.time(), "data": {
        "agents": {}, "totals": {}, "unresolved": [], "panel": panel, "panel_unresolved": [],
        "elsewhere": [], "window": {"start": day, "end": day}, "seats": 0}}
    A._V5_SMS_INFLIGHT.clear()
    A._V5_SMS_AGAIN.clear()

q = "start=%s&end=%s&tz=%d" % (day, day, tz)
try:
    r = c.get("/api/teams/inbox?" + q)
    ck("no gate at all: 401", r.status_code == 401, r.status_code)
    r = c.get("/api/teams/inbox?" + q + "&k=" + A._teams_token())
    ck("the /teams key opens it", r.status_code == 200, (r.status_code, r.data[:120]))
    j = r.get_json() or {}
    ck("every shared line is listed, Anna included, in the panel's order",
       [x["label"] for x in j.get("lines", [])] == ["Ariel", "Salome", "Artem", "Umed", "Anna"], j.get("lines"))
    t = j.get("totals") or {}
    ck("totals add the KNOWN lines only (Umed, refused, is left out)",
       (t.get("unread"), t.get("unanswered"), t.get("received"), t.get("sent")) == (1 + 0 + 1 + 4, 2 + 0 + 2 + 1, 20, 12), t)
    ck("...and the refused line is counted apart", t.get("unknown_lines") == 1 and t.get("lines") == 5, t)
    ck("a fresh cache is not re-read", not A._V5_SMS_INFLIGHT, A._V5_SMS_INFLIGHT)
    ck("it is served from the cache (age, not loading)", "age_seconds" in j and not j.get("loading"), j.keys())

    # the sales board's own panel hides the teams-only line
    with c.session_transaction() as s:
        s["v5_pw"] = True
    r5 = c.get("/api/v5/sms?" + q)
    j5 = r5.get_json() or {}
    ck("/api/v5/sms still answers", r5.status_code == 200, (r5.status_code, r5.data[:120]))
    ck("Anna is NOT on the sales board's panel", [x["label"] for x in j5.get("panel", [])] == ["Ariel", "Salome", "Artem", "Umed"],
       [x.get("label") for x in j5.get("panel", [])])

    # an unread window: loading, and the build is started once
    started = []
    _real_thread = A.threading.Thread
    class _T:
        def __init__(self, *a, **k): started.append(k.get("name"))
        def start(self): pass
    A.threading.Thread = _T
    try:
        other = (today.date() - A.timedelta(days=1)).isoformat()
        r = c.get("/api/teams/inbox?start=%s&end=%s&tz=%d&k=%s" % (other, other, tz, A._teams_token()))
        j = r.get_json() or {}
        ck("a window never read says loading, never an empty inbox", j.get("loading") is True and j.get("lines") == [], j)
        ck("...and starts the one shared build", started == ["v5sms:build"], started)
        r = c.get("/api/teams/inbox?start=%s&end=%s&tz=%d&k=%s" % (other, other, tz, A._teams_token()))
        ck("a second ask while it runs does not start another", started == ["v5sms:build"], started)
    finally:
        A.threading.Thread = _real_thread
        with A._V5_SMS_LOCK:
            A._V5_SMS_INFLIGHT.clear()
finally:
    if _real_cfg is not None:
        _cls.configured = _real_cfg
    else:
        try:
            delattr(_cls, "configured")
        except AttributeError:
            pass
    with A._V5_SMS_LOCK:
        A._V5_SMS_CACHE.clear()

for e in errors:
    print("  FAIL " + e)
print("%d passed, %d failed" % (passed, len(errors)))
sys.exit(1 if errors else 0)
