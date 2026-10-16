"""Do-not-call dispositions, honored -- who gets unsubscribed, who goes to
review, and exactly what is written.

Danny, 2026-10-15: "All those do not call should've been unsubscribed and
triggered correct flows in our CRM" / "Fix all of these and then figure out a
way to fix previous ones".
Run: ./.venv/bin/python -B checks_dnc.py   (tally LAST)
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dnc_dispositions as dd  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


T = "2026-10-15T12:00:00-04:00"        # the DNC
BEFORE = "2026-10-10T09:00:00-04:00"
AFTER = "2026-10-15T15:00:00-04:00"


def call(i, who=None, deal=None, mod=None, at=T, subj="Outbound Call to +13055550100"):
    return {"id": i, "Who_Id": {"id": who} if who else None, "What_Id": {"id": deal} if deal else None,
            "$se_module": mod or ("Deals" if deal else ("Contacts" if who else "Home")),
            "Call_Start_Time": at, "Subject": subj}


def deal(i, contact, stage, created=BEFORE, **kw):
    d = {"id": i, "Contact_Name": {"id": contact} if contact else None, "Stage": stage, "Created_Time": created,
         "RC_SMS_Opt_Out": False, "Send_Welcome_SMS": True, "Send_No_Show_SMS": False,
         "Send_Photo_Request_SMS": False, "New_Deal_24H_SMS": True, "Short_Call_SMS": True}
    d.update(kw)
    return d


def contact(i, dnc=False, sms=False, email=False, unsub=None, created=BEFORE):
    return {"id": i, "Do_Not_Contact": dnc, "ringcentralextensionabr__SMS_OPT_OUT": sms, "Email_Opt_Out": email,
            "Unsubscribed_Time": unsub, "Created_Time": created}


def other(i, who=None, deal=None, at=AFTER, typ="Outbound", disp=None, status="Completed"):
    return {"id": i, "Who_Id": {"id": who} if who else None, "What_Id": {"id": deal} if deal else None,
            "Call_Type": typ, "Call_Start_Time": at, "Outgoing_call_disposition": disp, "Outgoing_Call_Status": status}


def world(dnc_calls, deals=(), contacts=(), journeys=None, calls=(), numbers=None):
    return {"dnc_calls": list(dnc_calls), "deals": {d["id"]: d for d in deals},
            "contacts": {c["id"]: c for c in contacts}, "journeys": journeys or {},
            "calls": {c["id"]: c for c in calls}, "numbers": numbers or {}}


def one(w, key):
    return next(d for d in dd.decide(w) if d["key"] == key)


# ---- the plain case: an Expired deal, a contact with nothing set, old cadence calls open ----
w = world([call("k1", who="c1", deal="d1")],
          deals=[deal("d1", "c1", "Expired"), deal("d2", "c1", "Call Scheduled"), deal("d3", "c1", "Unsubscribe")],
          contacts=[contact("c1")],
          calls=[other("o1", who="c1", deal="d2", at="2026-07-01T10:00:00-04:00", status="Overdue"),
                 other("o2", deal="d1", at="2026-10-20T10:00:00-04:00", status="Scheduled"),
                 other("o3", who="c1", at="2026-07-01T10:00:00-04:00", status="Completed", disp="No Answer")])
d = one(w, "c1")
ck("a DNC on an Expired deal is applied (the Zoho rule skipped Expired)", d["result"] == "apply", d)
ck("every open sales deal of the person moves to Unsubscribe, not only the dialled one", d["deals_move"] == ["d1", "d2"], d["deals_move"])
ck("a deal already on Unsubscribe gets the switches only", d["deals_flag"] == ["d3"], d["deals_flag"])
ck("the Contact is written", d["contact_write"] is True, d)
ck("every Scheduled / Overdue call on the person or their deals is cancelled, a completed one is not",
   d["calls_cancel"] == ["o1", "o2"], d["calls_cancel"])
p = dd.payloads([d], w, "2026-10-16T01:00:00-04:00")
ck("the Contact gets Do_Not_Contact, the SMS and email opt-outs, Manual and the time",
   p["contacts"] == [{"id": "c1", "Do_Not_Contact": True, "ringcentralextensionabr__SMS_OPT_OUT": True,
                      "Email_Opt_Out": True, "Unsubscribed_Mode": "Manual",
                      "Unsubscribed_Time": "2026-10-16T01:00:00-04:00", "Deal_Stage": "Unsubscribe"}], p["contacts"])
ck("a moved deal gets Stage Unsubscribe, RC_SMS_Opt_Out and every Send_*_SMS off",
   p["deals_move"][0] == dict({"id": "d1", "Stage": "Unsubscribe"}, **dd.SEND_OFF) and all(r["Stage"] == "Unsubscribe" for r in p["deals_move"]),
   p["deals_move"])
ck("a flag-only deal never carries a Stage", p["deals_flag"] == [dict({"id": "d3"}, **dd.SEND_OFF)], p["deals_flag"])
ck("a cancelled call says why, with the DNC date", p["calls_cancel"][0]["Outgoing_Call_Status"] == "Cancelled"
   and p["calls_cancel"][0]["_note"] == "Cancelled: do not call (DNC disposition 2026-10-15).", p["calls_cancel"])

ck("the Contact's Deal_Stage copy says Unsubscribe once its deals moved (the stage rule would have written it)",
   p["contacts"][0].get("Deal_Stage") == "Unsubscribe" and one(w, "c1").get("contact_stage") is True, p["contacts"])
_wm = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired")],
            contacts=[dict(contact("c1", dnc=True, sms=True, email=True), Deal_Stage="Expired")])
_dm = one(_wm, "c1")
ck("a flagged Contact whose deal moves is still written, for the stage copy alone",
   _dm["contact_write"] and _dm.get("contact_stage") and dd.payloads([_dm], _wm, "N")["contacts"][0]["Deal_Stage"] == "Unsubscribe", _dm)
_wd = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Unsubscribe", **dd.SEND_OFF)],
            contacts=[dict(contact("c1", dnc=True, sms=True, email=True), Deal_Stage="Unsubscribe")])
ck("...and one already showing Unsubscribe is not", one(_wd, "c1")["result"] == "done", one(_wd, "c1"))
c2 = contact("c2", unsub="2026-09-01T10:00:00-04:00")
p2 = dd.payloads([one(world([call("k", who="c2")], contacts=[c2]), "c2")], world([call("k", who="c2")], contacts=[c2]), "X")
ck("an existing Unsubscribed_Time is kept", "Unsubscribed_Time" not in p2["contacts"][0], p2["contacts"])

# ---- already done: nothing to write ----
w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Unsubscribe", **{**dd.SEND_OFF})],
          contacts=[dict(contact("c1", dnc=True, sms=True, email=True), Deal_Stage="Unsubscribe")])
d = one(w, "c1")
ck("someone already fully unsubscribed is 'done' and nothing is written", d["result"] == "done" and not
   any(dd.payloads([d], w, "X").values()), d)

# ---- never on a patient ----
for stage in ("Closed Won - Surgery Scheduled", "Retainer Invoice Sent", "Payment Received, Needs Surgery Scheduled",
              "Goals Financing Program", "Cancelled or Disputed", "Some Stage Nobody Listed"):
    w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired"), deal("d9", "c1", stage)],
              contacts=[contact("c1")])
    d = one(w, "c1")
    ck("a person with a deal in %r goes to review, nothing written" % stage,
       d["result"] == "review" and "patient stage" in d["reason"] and not any(dd.payloads([d], w, "X").values()), d)
w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired")], contacts=[contact("c1")],
          journeys={"c1": ["j1"]})
ck("an Active Journey goes to review", one(w, "c1")["result"] == "review" and one(w, "c1")["reason"] == "Active Journey", one(w, "c1"))

# ---- never over a newer yes ----
w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired"), deal("d2", "c1", "New Deal", created=AFTER)],
          contacts=[contact("c1")])
ck("a deal created after the DNC (they asked again) goes to review", one(w, "c1")["reason"] == "a deal was created after the DNC", one(w, "c1"))
for typ in ("Inbound", "Missed"):
    w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired")], contacts=[contact("c1")],
              calls=[other("i1", who="c1", typ=typ)])
    ck("an %s call after the DNC goes to review" % typ, one(w, "c1")["reason"] == "they called in after the DNC", one(w, "c1"))
for disp in ("Follow Up Scheduled", "Quote Sent", "Other / Not Listed", "Price Shopping/Not Ready"):
    w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired")], contacts=[contact("c1")],
              calls=[other("x1", deal="d1", disp=disp)])
    ck("a later %r goes to review" % disp, one(w, "c1")["reason"] == "a later call reached them", one(w, "c1"))
for disp in ("No Answer", "Voicemail / No Answer", "Answering Machine", "Disposition Timeout", "Wrong Number",
             "[No-Agent-Disp]", "Bad Connection/Don't Hear", "No Disposition", "DNC (Do Not Call)", None):
    w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired")], contacts=[contact("c1")],
              calls=[other("x1", deal="d1", disp=disp)])
    ck("a later %r does not undo the DNC" % disp, one(w, "c1")["result"] == "apply", one(w, "c1"))
w = world([call("k1", who="c1", deal="d1")], deals=[deal("d1", "c1", "Expired")], contacts=[contact("c1")],
          calls=[other("x1", deal="d1", disp="Call Scheduled", at=BEFORE), other("i1", who="c1", typ="Inbound", at=BEFORE)])
ck("a conversation or call-in BEFORE the DNC does not block it", one(w, "c1")["result"] == "apply", one(w, "c1"))
w = world([call("k1", who="c1", deal="d1"), call("k2", who="c1", deal="d1", at="2026-10-15T16:00:00-04:00")],
          deals=[deal("d1", "c1", "Expired")], contacts=[contact("c1")],
          calls=[other("x1", deal="d1", disp="Call Scheduled", at=AFTER)])
d = one(w, "c1")
ck("the LATEST DNC is what counts: a conversation between two DNCs does not block the second",
   d["result"] == "apply" and d["dnc_calls"] == ["k1", "k2"] and d["dnc_at"].startswith("2026-10-15T16:00"), d)

# ---- calls with no contact, numbers with no record ----
w = world([call("k1", deal="d1")], deals=[deal("d1", None, "Call Scheduled")])
d = one(w, "deal:d1")
ck("a deal with no Contact still moves", d["result"] == "apply" and d["deals_move"] == ["d1"] and not d["contact_write"], d)
w = world([call("k1")], numbers={"3055550100": []})
ck("an unlinked call whose number is nowhere in the CRM is 'no_record'", one(w, "num:3055550100")["result"] == "no_record", dd.decide(w))
w = world([call("k1")], contacts=[contact("c7")], deals=[deal("d7", "c7", "Expired")], numbers={"3055550100": ["c7"]})
ck("an unlinked call whose number matches a Contact is applied to that Contact", one(w, "c7")["result"] == "apply"
   and one(w, "c7")["deals_move"] == ["d7"], dd.decide(w))
w = world([call("k1")], contacts=[contact("c7", created=AFTER)], deals=[], numbers={"3055550100": ["c7"]})
ck("...unless that Contact was created after the DNC (a new inquiry)",
   one(w, "c7")["reason"] == "the Contact was created after the DNC", dd.decide(w))
ck("the number comes from the Subject, ten digits", dd.number_of({"Subject": "Outbound Call to +13055550100"}) == "3055550100"
   and dd.number_of({"Subject": "Inbound Call from anonymous caller"}) == "", "number_of")
w = world([call("k1", who="c1", mod="Contacts")], contacts=[contact("c1")], deals=[deal("d1", "c1", "Quote Sent")])
ck("a call linked to the Contact only still reaches the Contact's deals", one(w, "c1")["deals_move"] == ["d1"], one(w, "c1"))

# ---- collect: chunks of 50, the right modules ----
qs = []


def fake_coql(q):
    qs.append(q)
    if "from Deals where" in q:
        return [deal("d1", "c1", "Expired")]
    if "from Contacts where id in" in q:
        return [contact("c1")]
    return []


many = [call("k%d" % i, who="c%d" % i) for i in range(120)]
dd.collect(fake_coql, many)
ck("no `in` list is longer than 50 ids (COQL's limit)",
   all(q.split(" in (")[1].split(")")[0].count(",") < 50 for q in qs if " in (" in q), max(q.count(",") for q in qs))
ck("Active Journeys are read by Contact", any("from Journeys where (Contact.id in (" in q and "Case_Status = 'Active'" in q for q in qs), qs[-5:])
ck("calls are read by Contact AND by deal", any("from Calls where Who_Id in" in q for q in qs)
   and any("from Calls where What_Id in" in q for q in qs), [q[:60] for q in qs])

# ---- apply: workflows ON for the stage move only ----
seen = []


def fake_put(module, rows, trigger):
    seen.append((module, [r["id"] for r in rows], trigger, rows))
    return [(r["id"], r["id"] != "bad", "x") for r in rows]


plan = {"contacts": [{"id": "c1"}], "deals_move": [{"id": "d%d" % i, "Stage": "Unsubscribe"} for i in range(150)],
        "deals_flag": [{"id": "f1"}], "calls_cancel": [{"id": "o1", "Outgoing_Call_Status": "Cancelled", "_note": "N."}]}
res = dd.apply(fake_put, plan, describe=lambda ids: {"o1": "old notes"})
mods = [(m, t, len(ids)) for m, ids, t, _ in seen]
ck("the stage move is SILENT -- trigger: [] -- in chunks of 100 (Danny: no outbound messages)",
   ("Deals", [], 100) in mods and ("Deals", [], 50) in mods, mods)
ck("every write, of every kind, is trigger: []", all(t == [] for _, t, _ in mods) and ("Contacts", [], 1) in mods
   and ("Deals", [], 1) in mods and ("Calls", [], 1) in mods, mods)
ck("a cancelled call keeps its notes and gains one line, and no private key leaks into Zoho",
   seen[-1][3] == [{"id": "o1", "Outgoing_Call_Status": "Cancelled", "Description": "old notes\n\nN."}], seen[-1][3])
ck("the counts add up", res["deals_moved"] == 150 and res["contacts"] == 1 and res["calls_cancelled"] == 1 and not res["errors"], res)

# ---- an old pipeline without Unsubscribe: the deal keeps its stage, gets the switches ----
seen2 = []


def pipe_put(module, rows, trigger):
    seen2.append((module, rows))
    return [(r["id"], not (r.get("Stage") and r["id"] == "old"), "Pipeline doesn't contain the Stage") for r in rows]


res2 = dd.apply(pipe_put, {"contacts": [], "deals_move": [{"id": "old", "Stage": "Unsubscribe"}, {"id": "new", "Stage": "Unsubscribe"}],
                           "deals_flag": [], "calls_cancel": []})
ck("a deal whose pipeline has no Unsubscribe is retried with the switches only, not an error",
   res2["deals_moved"] == 1 and res2["deals_flagged"] == 1 and res2["stage_kept"] == 1 and not res2["errors"]
   and seen2[-1] == ("Deals", [dict({"id": "old"}, **dd.SEND_OFF)]), (res2, seen2))

# ---- the ledger: a settled call is not done twice; a failed write is retried ----
path = os.path.join(tempfile.mkdtemp(), "dnc.json")
st = dd.Store(path)
w = world([call("k1", who="c1", deal="d1"), call("k2", who="c2")], deals=[deal("d1", "c1", "Expired")],
          contacts=[contact("c1"), contact("c2")], journeys={"c2": ["j"]})
decs = dd.decide(w)
st.record(decs, {"errors": [{"module": "Deals", "id": "d1"}]}, "NOW")
st.save()
st2 = dd.Store(path)
ck("a person whose write failed stays pending; a review is settled",
   [c["id"] for c in st2.pending(w["dnc_calls"])] == ["k1"] and st2.data["calls"]["k2"]["result"] == "review", st2.data["calls"])
ck("the review list keeps the reason", st2.data["review"]["c2"]["reason"] == "Active Journey", st2.data["review"])
st2.record(dd.decide(w), {"errors": []}, "LATER")
ck("after a clean write the call is settled", st2.pending(w["dnc_calls"]) == [], st2.data["calls"]["k1"])
st2.data["armed"] = True
st2.save()
ck("the armed flag survives a restart", dd.Store(path).data["armed"] is True, dd.Store(path).data)
ck("the summary is counts only", set(dd.summarize(decs)) == {"people", "by_result", "deals_move", "deals_flag", "contacts", "calls_cancel"},
   dd.summarize(decs))

# ---- the app: the loop, the route, the kill switch ----
import app as A  # noqa: E402

ck("the 15-minute loop is one of the worker loops", any(n == "loop:dnc" for n, _ in A._WORKER_LOOPS), [n for n, _ in A._WORKER_LOOPS])
ck("the ledger lives on the data disk", A._DNC_FILE.name == "dnc_dispositions.json" and A._DNC_FILE.parent == A._OPTOUT_FILE.parent,
   str(A._DNC_FILE))
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
ck("writes need the arm AND the env switch; the control writes one person whatever the arm says",
   "armed = os.environ.get(\"DNC_ZOHO_WRITE\", \"1\") != \"0\" and bool(_dnc.data.get(\"armed\"))" in _src
   and "write = armed if write is None else (bool(write) and armed)" in _src, "gates")
_c = A.app.test_client()
_real = A._v6_allowed
A._v6_allowed = lambda: False
try:
    r401 = _c.get("/api/v6/dnc")
finally:
    A._v6_allowed = _real
ck("the route is signed-in only", r401.status_code == 401, r401.status_code)

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
