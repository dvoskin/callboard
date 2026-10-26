"""The shared office lines, watched every ten minutes (Danny, 2026-10-25: "We have
to close this leak"): a STOP there is marked at once, and every text SENT from
those lines to a number that opted out -- our STOP list or Zoho DNC -- is
recorded, alerted once, and counted on /teams' Shared Inbox.
Run: .venv/bin/python checks_shared_lines_watch.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("COORD_LINK_SECRET", "teams-key-for-checks")
import app as A  # noqa: E402

passed, errors = 0, []
def ck(label, cond, got=None):
    global passed
    if cond: passed += 1
    else: errors.append("%s -- got %r" % (label, got))

OPT = A._optouts_mod
asked, alerts = [], []
_saved = (A._sms_panel_seats, OPT.scan, A._zoho_optout, A._telegram.send_message, dict(A._optouts.data), OPT.mark_in_zoho)
A._sms_panel_seats = lambda: ([{"label": "Salome", "name": "Salome", "ext": "151", "ext_id": "220135049"},
                               {"label": "Ariel", "name": "Ariel Voskin", "ext": "689", "ext_id": "680145035"}], [])
NOW = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
_now = _dt.now(_tz.utc).replace(microsecond=0)
T = lambda minutes: (_now + _td(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")   # RingCentral's format
def _scan(fetch, seats, start_dt, end_dt, max_pages=4, timeout=15.0, pause=0.3):
    asked.append([s["ext_id"] for s in seats])
    hits = [{"digits": "3055550111", "seat": "Salome", "ext": "151", "at": NOW[:-2] + "01", "text": "STP", "msg_id": "m1"}]
    outbound = [{"digits": "3055550111", "seat": "Ariel", "at": NOW[:-2] + "05", "status": "Delivered"},   # texted AFTER her STOP
                {"digits": "3055550222", "seat": "Salome", "at": NOW[:-2] + "06", "status": "Delivered"},  # DNC in Zoho, never texted STOP
                {"digits": "3055550333", "seat": "Salome", "at": NOW[:-2] + "07", "status": "Delivered"},  # clean
                # 2026-10-26, Veronica: texted at 14:06, STOP 14:16, Do_Not_Contact set 14:20 -> NOT a leak
                {"digits": "3055550444", "seat": "Salome", "at": T(-14), "status": "Delivered"},
                # opted out, but Zoho can't say when (only that it was by 5 minutes after the text) -> listed, not alerted
                {"digits": "3055550555", "seat": "Ariel", "at": T(-10), "status": "Delivered"}]
    return hits, [], outbound
OPT.scan = _scan
OPTOUT = {"3055550222": {"by": _now - _td(hours=4), "exact": True},     # DNC since this morning, texted now -> leak
          "3055550444": {"by": _now, "exact": True},                       # DNC set AFTER the text -> not a leak
          "3055550555": {"by": _now - _td(minutes=5), "exact": False}}     # only a bound, and it's after the text -> unconfirmed
A._zoho_optout = lambda digits: OPTOUT.get(digits)
A._telegram.send_message = lambda text, sender_name="", reply_to_message_id=None: alerts.append((sender_name, text)) or {"ok": True}
OPT.mark_in_zoho = lambda *a, **k: {"marked": True}
A._optouts.data["numbers"] = {}
A._optouts.data["zoho_armed"] = False
with A._LEAKS["lock"]:
    A._LEAKS["hits"] = []; A._LEAKS["unconfirmed"] = []
A._LEAKS["zoho_dnc"] = {}
A.app.config["TESTING"] = True
c = A.app.test_client()
_cls = type(A._ringcx); _real_cfg = _cls.__dict__.get("configured"); _cls.configured = property(lambda self: True)
try:
    info = A._shared_lines_watch(alert=True)
    ck("the watch reads the shared lines, max two pages", asked == [["220135049", "680145035"]], asked)
    ck("a STOP on a shared line lands on the list at once", "3055550111" in A._optouts.data["numbers"], list(A._optouts.data["numbers"]))
    ck("a text sent after the STOP is a leak, and a text to a Zoho DNC number is a leak; the clean one is not",
       sorted((o["digits"], o["why"]) for o in A._leaks_recent(24)) == [("3055550111", "stop_list"), ("3055550222", "zoho_dnc")], A._leaks_recent(24))
    ck("one Telegram alert, from the DNC watch, masking the numbers",
       len(alerts) == 1 and alerts[0][0] == "DNC watch" and "***-0111" in alerts[0][1] and "3055550111" not in alerts[0][1] and "Zoho" in alerts[0][1], alerts)
    ck("the pass reports itself", info.get("new_leaks") == 2 and info.get("new_stops") == 1 and info.get("unconfirmed") == 1 and not info.get("error"), info)
    ck("a text sent BEFORE the opt-out is not a leak and is not even listed (Veronica, 10-26)",
       "3055550444" not in [o["digits"] for o in A._leaks_recent(24) + A._leaks_unconfirmed(24)], A._leaks_recent(24))
    ck("when Zoho can't prove which came first, the text is listed as unconfirmed and never alerted",
       [o["digits"] for o in A._leaks_unconfirmed(24)] == ["3055550555"] and all("0555" not in a[1] for a in alerts), (A._leaks_unconfirmed(24), alerts))
    A._shared_lines_watch(alert=True)
    ck("a second pass does not alert the same leaks again", len(alerts) == 1, len(alerts))
    ck("...and does not double count them", len(A._leaks_recent(24)) == 2, A._leaks_recent(24))
    # /teams carries the count
    day = time.strftime("%Y-%m-%d")
    with A._V5_SMS_LOCK:
        A._V5_SMS_CACHE.clear()
        A._TEAMS_INBOX_PARTIAL[(day, day, 240)] = {"at": time.time(), "panel": [{"label": "Ariel", "unread": 0, "unanswered": 0, "sent": 1, "received": 0, "complete": True, "unknown": False}],
                                                   "panel_unresolved": [], "window": {"start": day, "end": day}}
        A._V5_SMS_INFLIGHT.add((day, day, 240))
    r = c.get("/api/teams/inbox?start=%s&end=%s&tz=240&k=%s" % (day, day, A._teams_token()))
    j = r.get_json() or {}
    ck("/api/teams/inbox carries the 24 h leak count and masked rows",
       j.get("leaks_24h") == 2 and all(len(x["last4"]) == 4 and "digits" not in x for x in j.get("leaks", [])), (j.get("leaks_24h"), j.get("leaks")))
    ck("/api/teams/inbox carries the unconfirmed count apart", j.get("leaks_unconfirmed_24h") == 1, j.get("leaks_unconfirmed_24h"))
finally:
    A._sms_panel_seats, OPT.scan, A._zoho_optout, A._telegram.send_message, data, OPT.mark_in_zoho = _saved
    A._optouts.data.clear(); A._optouts.data.update(data)
    with A._LEAKS["lock"]:
        A._LEAKS["hits"] = []; A._LEAKS["unconfirmed"] = []
    with A._V5_SMS_LOCK:
        A._V5_SMS_INFLIGHT.clear(); A._TEAMS_INBOX_PARTIAL.clear()
    if _real_cfg is not None: _cls.configured = _real_cfg
    else:
        try: delattr(_cls, "configured")
        except AttributeError: pass
for e in errors: print("  FAIL " + e)
print("%d passed, %d failed" % (passed, len(errors)))
sys.exit(1 if errors else 0)
