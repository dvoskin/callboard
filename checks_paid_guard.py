"""Paid retainers, honored -- which payment moves a deal, which only gets the
box, which goes to a person, and exactly what is written.

Danny, 2026-10-19: "Getting complaints that some patients who pay their
retainer invoices on Zoho, don't have their corresponding deals move to the
correct stage" / "Fix them all fully. Make sure it doesn't happen again too".
Run: ./.venv/bin/python -B checks_paid_guard.py   (tally LAST)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paid_guard as pg  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


NOW = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)      # 12:00 in New York
PAID = "2026-10-18T10:40:51-0400"                              # Books' own format
PR = pg.PAYMENT_RECEIVED


def pay(i="p1", rid="901", at=PAID, **kw):
    p = {"payment_id": i, "payment_number": "84001", "retainerinvoice_id": rid, "date": at[:10], "amount": 500,
         "customer_id": "3136880000161627632", "customer_name": "Pat Example", "created_time": at, "last_modified_time": at,
         "payment_type": "Retainer Payment", "payment_status": "paid", "bcy_refunded_amount": 0, "description": ""}
    p.update(kw)
    return p


def deal(i="d1", stage="Retainer Invoice Sent", paid=False, contact="k1", **kw):
    d = {"id": i, "Deal_Name": "FlexSculpt: Nov 20, 2026", "Stage": stage, "Retainer_Paid": paid,
         "Retainer_Paid_Date": None, "Retainer_Invoice_URL": "https://books.zoho.com/app#/retainerinvoices/901",
         "Payment_Method": "Out of Pocket", "Estimate_Sent": True,
         "Contact_Name": {"name": "Pat Example", "id": contact} if contact else None, "Scheduling_Owner": None,
         "Created_Time": "2026-10-01T09:00:00-04:00", "Modified_Time": "2026-10-18T11:00:00-04:00"}
    d.update(kw)
    return d


def journey(contact="k1", status="Active", created="2026-10-18T12:00:00-04:00", deal_id=None, name="Pat Example"):
    return {"id": "j-" + str(contact), "Name": name, "Case_Status": status, "Contact": {"id": contact} if contact else None,
            "Deal": {"id": deal_id} if deal_id else None, "Created_Time": created}


# ---- reading Zoho and Books ------------------------------------------------
ck("Books' -0400 offset reads", pg._t("2026-10-18T10:40:51-0400") == datetime(2026, 10, 18, 14, 40, 51, tzinfo=timezone.utc),
   pg._t("2026-10-18T10:40:51-0400"))
ck("Zoho's -04:00 offset reads", pg._t("2026-10-18T10:40:51-04:00") == pg._t("2026-10-18T10:40:51-0400"))
ck("blank and junk read as None", pg._t("") is None and pg._t("soon") is None)
ck("the retainer id comes out of either URL shape",
   pg.retainer_id("https://books.zoho.com/app/773501182#/retainerinvoices/3136880000161662925") == "3136880000161662925"
   and pg.retainer_id("https://books.zoho.com/app#/retainerinvoices/901") == "901" and pg.retainer_id(None) == "")
ck("Goals Financing goes to its own stage", pg.target_stage("Goals Financing") == pg.GOALS_FINANCING
   and pg.target_stage("Out of Pocket") == PR and pg.target_stage(None) == PR)
ck("only retainer payments are looked at", pg.is_retainer_payment(pay())
   and not pg.is_retainer_payment(pay(payment_type="Invoice Payment"))
   and not pg.is_retainer_payment(pay(retainerinvoice_id="")))
ck("a refund is skipped", pg.skip_reason(pay(bcy_refunded_amount=500)) == "refunded")
ck("the training account and 'test' payments are skipped",
   pg.skip_reason(pay(customer_name="Luisa Training")) == "test" and pg.skip_reason(pay(description=" Test ")) == "test"
   and pg.skip_reason(pay()) is None)
ck("the stage lists do not overlap", not (pg.PAID_STAGES & pg.MOVE_STAGES), pg.PAID_STAGES & pg.MOVE_STAGES)
ck("a stage a person chose is never moved automatically",
   not ({"ON HOLD", "DND", "Duplicates / Junk", "Cancelled or Disputed", "Lost / Closed"} & pg.MOVE_STAGES))
ck("the bare deals the dedup keeps are movable", {"New Deal", "New Deal / Instagram", pg.UNSUB} <= pg.MOVE_STAGES)

# ---- decide_payment --------------------------------------------------------
d = pg.decide_payment(pay(at="2026-10-19T11:45:00-0400"), deal(), False, NOW)
ck("a payment inside the grace period waits for the normal flow", d["result"] == "wait", d)
d = pg.decide_payment(pay(), deal(), False, NOW)
ck("the stranded payment moves the deal", d["result"] == "fix" and d["deal"] == "d1", d)
ck("... to Payment Received, box ticked, dated when Books took the money",
   d["write"] == {"Stage": PR, "Retainer_Paid": True, "Retainer_Paid_Date": "2026-10-18T10:40:51-04:00"}, d["write"])
ck("... WITH workflows, so the coordinator is assigned", d["trigger"] == ["workflow"], d["trigger"])
ck("... and the note says what happened", "84001" in d["note"] and "Retainer Invoice Sent" in d["note"], d["note"])
d = pg.decide_payment(pay(), deal(Payment_Method="Goals Financing"), False, NOW)
ck("a Goals Financing deal goes to Goals Financing Program", d["write"].get("Stage") == pg.GOALS_FINANCING, d["write"])
d = pg.decide_payment(pay(), deal(stage=PR, paid=True), False, NOW)
ck("a deal the payment reached is ok", d["result"] == "ok" and not d["write"], d)
d = pg.decide_payment(pay(), deal(stage="Closed Won - Surgery Scheduled", paid=False), False, NOW)
ck("a paid stage with the box unticked gets the box only, silently",
   d["result"] == "flag" and "Stage" not in d["write"] and d["write"]["Retainer_Paid"] is True and d["trigger"] == [], d)
d = pg.decide_payment(pay(), deal(), True, NOW)
ck("already with a coordinator: the box, no stage move, no workflows",
   d["result"] == "flag" and "Stage" not in d["write"] and d["trigger"] == [], d)
d = pg.decide_payment(pay(), deal(stage=pg.UNSUB, paid=True), True, NOW)
ck("already with a coordinator and already ticked: nothing to write", d["result"] == "ok" and not d["write"], d)
d = pg.decide_payment(pay(), deal(stage=pg.UNSUB, paid=True, Retainer_Paid_Date="2026-10-18T10:41:00-04:00"), False, NOW)
ck("paid, then a STOP: back to Payment Received, the paid date left alone",
   d["result"] == "fix" and d["write"] == {"Stage": PR} and d["trigger"] == ["workflow"], d)
for st in ("ON HOLD", "Duplicates / Junk", "Cancelled or Disputed", "Folli", "Some New Stage"):
    d = pg.decide_payment(pay(), deal(stage=st), False, NOW)
    ck("stage %s goes to a person" % st, d["result"] == "review" and not d["write"], d)
d = pg.decide_payment(pay(), None, False, NOW, candidate="d9")
ck("no deal carries the retainer: a person, shown the customer's deal, nothing written",
   d["result"] == "review" and d["deal"] == "d9" and not d["write"], d)
d = pg.decide_payment(pay(bcy_refunded_amount=500), deal(), False, NOW)
ck("a refunded payment moves nothing", d["result"] == "skip", d)

# ---- pick_deal -------------------------------------------------------------
ck("one deal carries the retainer", pg.pick_deal([deal()]) == (deal(), None))
ck("of two, the paid one", pg.pick_deal([deal("a"), deal("b", stage=PR, paid=True)])[0]["id"] == "b")
ck("two unpaid: a person", pg.pick_deal([deal("a"), deal("b")]) == (None, "2 deals carry this retainer"))
ck("none: (None, None)", pg.pick_deal([]) == (None, None))

# ---- handled ---------------------------------------------------------------
SINCE = pg._t(PAID)
ck("an Active Journey", pg.handled(["k1"], [], [journey()], SINCE))
ck("a Journey with no Contact, found by its Deal (the contact merge orphans them)",
   pg.handled(["k1"], [deal()], [journey(contact=None, deal_id="d1")], SINCE, exclude_deal="d1"))
ck("a Journey on THIS deal counts too", pg.handled(["k1"], [], [journey(contact=None, deal_id="d1")], SINCE, exclude_deal="d1"))
ck("India Smith: a closed Journey from an earlier purchase does not count",
   not pg.handled(["k1"], [], [journey(status="Closed", created="2023-05-01T10:00:00-04:00")], SINCE))
ck("a Journey begun for this payment does, closed or not",
   pg.handled(["k1"], [], [journey(status="Closed", created="2026-10-18T12:00:00-04:00")], SINCE))
ck("someone else's Journey does not", not pg.handled(["k1"], [], [journey(contact="k2")], SINCE))
ck("another deal paid for this purchase",
   pg.handled(["k1"], [deal("d2", stage=PR, paid=True, Retainer_Paid_Date="2026-10-18T09:00:00-04:00")], [], SINCE,
              exclude_deal="d1"))
ck("a Closed Won deal from 2023 does not",
   not pg.handled(["k1"], [deal("d2", stage="Closed Won - Surgery Scheduled", paid=True,
                               Retainer_Paid_Date="2023-01-10T09:00:00-05:00", Created_Time="2023-01-01T09:00:00-05:00")],
                  [], SINCE, exclude_deal="d1"))
ck("the deal itself never makes itself handled",
   not pg.handled(["k1"], [deal("d1", stage=PR, paid=True)], [], SINCE, exclude_deal="d1"))

# ---- same name, another contact record (Shicresia Metoyer) ------------------
ck("an Active Journey under the same name", pg.same_name([journey(contact="k7", name="pat  EXAMPLE")], "Pat Example", SINCE))
ck("a closed old one does not",
   not pg.same_name([journey(contact="k7", status="Closed", created="2024-01-01T10:00:00-05:00")], "Pat Example", SINCE))
ck("a contact named Unknown, or one word, matches nothing",
   not pg.same_name([journey(name="Unknown")], "Unknown", SINCE) and not pg.same_name([journey(name="Pat")], "Pat", SINCE))
ck("a different name does not", not pg.same_name([journey(name="Pat Examples")], "Pat Example", SINCE))
ck("a Journey named last-first is the same person", pg.same_name([journey(contact="k7", name="Example Pat")], "Pat Example", SINCE))
d = pg.hold_for_name(pg.decide_payment(pay(), deal(), False, NOW), True)
ck("a name match holds the move for a person", d["result"] == "review" and not d["write"], d)
d = pg.hold_for_name(pg.decide_payment(pay(), deal(stage=PR, paid=False), False, NOW), True)
ck("... but not the silent box", d["result"] == "flag", d)

# ---- merges ----------------------------------------------------------------
SNAP = deal("old1", stage=PR, paid=True, Retainer_Paid_Date="2026-10-17T10:00:00-04:00", Amount=9800,
            Procedure="FlexSculpt", Promos_Description="Fall promo", Scheduling_Owner={"name": "Mier", "id": "u5"},
            Owner={"id": "u1"}, Retainer_Sent=True, Invoice_URL="https://books.zoho.com/app#/invoices/77",
            Zoho_Books_Customer_ID="c1")
note = ("AUTO-MERGED duplicate Deal (deleted)\nOld DealID old1\nFULL RECORD JSON:" +
        json.dumps(SNAP).replace("Fall promo", "Fall\npromo"))
ck("the snapshot reads, raw newlines and all", (pg.parse_snapshot(note) or {}).get("Promos_Description") == "Fall\npromo")
ck("a note with no snapshot reads as None", pg.parse_snapshot("Merged duplicate") is None and pg.parse_snapshot(None) is None)
BARE = deal("new1", stage="New Deal / Instagram", Estimate_Sent=False, Retainer_Invoice_URL=None, Deal_Name="Pat Example")
d = pg.decide_merge(SNAP, BARE, False)
ck("a paid deal merged into a bare intake: the survivor becomes the paid deal", d["result"] == "fix" and d["deal"] == "new1"
   and d["write"]["Stage"] == PR and d["write"]["Retainer_Paid"] is True
   and d["write"]["Retainer_Paid_Date"] == "2026-10-17T10:00:00-04:00" and d["trigger"] == ["workflow"], d)
ck("... with the quote, the payment links and the coordinator",
   d["write"].get("Amount") == 9800 and d["write"].get("Deal_Name") == SNAP["Deal_Name"]
   and d["write"].get("Invoice_URL") and d["write"].get("Retainer_Invoice_URL")
   and d["write"].get("Scheduling_Owner") == {"id": "u5"}, d["write"])
ck("... never the owner or the contact: those are the survivor's",
   "Owner" not in d["write"] and "Contact_Name" not in d["write"], sorted(d["write"]))
d = pg.decide_merge(SNAP, dict(BARE, Estimate_Sent=True), False)
ck("a survivor with its own quote keeps it; the payment still comes over",
   d["result"] == "fix" and "Amount" not in d["write"] and "Deal_Name" not in d["write"] and d["write"].get("Invoice_URL"), d)
ck("an unpaid deleted deal is no business of this check",
   pg.decide_merge(dict(SNAP, Retainer_Paid=False), BARE, False)["result"] == "skip")
ck("a deleted Closed Won deal already had its Journey",
   pg.decide_merge(dict(SNAP, Stage="Closed Won - Surgery Scheduled"), BARE, False)["result"] == "skip")
ck("a survivor already paid is ok", pg.decide_merge(SNAP, dict(BARE, Stage=PR), False)["result"] == "ok")
ck("a patient already with a coordinator is ok", pg.decide_merge(SNAP, BARE, True)["result"] == "ok")
ck("a survivor that is gone too goes to a person", pg.decide_merge(SNAP, None, False)["result"] == "review")
ck("a survivor ON HOLD goes to a person", pg.decide_merge(SNAP, dict(BARE, Stage="ON HOLD"), False)["result"] == "review")
U = dict(SNAP, Stage=pg.UNSUB)
ck("a deleted paid deal on Unsubscribe, paid recently, is brought back",
   pg.decide_merge(U, BARE, False, NOW)["result"] == "fix")
ck("... only when the payment is recent",
   pg.decide_merge(dict(U, Retainer_Paid_Date="2025-01-01T10:00:00-05:00"), BARE, False, NOW)["result"] == "skip")
d = pg.decide_payment_merged(pay(), dict(SNAP, Stage="Retainer Invoice Sent", Retainer_Paid=False), BARE, False, NOW)
ck("a payment on a deal already merged away lands on the survivor, dated by Books",
   d["result"] == "fix" and d["deal"] == "new1" and d["write"]["Retainer_Paid_Date"] == "2026-10-18T10:40:51-04:00"
   and d["write"]["Stage"] == PR and "84001" in d["note"], d)
d = pg.decide_payment_merged(pay(), dict(SNAP, Stage="Duplicates / Junk"), BARE, False, NOW)
ck("... unless the merged deal sat in a stage nobody listed", d["result"] == "review" and not d["write"], d)

# ---- decide_unsub ----------------------------------------------------------
UD = deal("u1", stage=pg.UNSUB, paid=True, Retainer_Paid_Date="2026-09-01T12:20:20-04:00", RC_SMS_Opt_Out=True)
d = pg.decide_unsub(UD, False, NOW)
ck("a paid deal put on Unsubscribe goes back to Payment Received", d["result"] == "fix" and d["trigger"] == ["workflow"], d)
ck("... and only the stage: the text opt-out stays", d["write"] == {"Stage": PR}, d["write"])
ck("a patient with a coordinator is left alone", pg.decide_unsub(UD, True, NOW)["result"] == "ok")
ck("a 2024 payment is a past patient who opted out",
   pg.decide_unsub(dict(UD, Retainer_Paid_Date="2024-08-01T10:00:00-04:00"), False, NOW)["result"] == "skip")
ck("no paid date: left alone", pg.decide_unsub(dict(UD, Retainer_Paid_Date=None), False, NOW)["result"] == "skip")

ck("summarize counts reviews by reason", pg.summarize([pg._decision("review", "stage X"), pg._decision("ok", ""),
                                                       pg._decision("ok", "")]) == {"ok": 2, "review: stage X": 1})

# ---- a paid deal with no working coordinator (Ana Castro) --------------------
def sched(name, uid, start=10, end=19, off=("Sat", "Sun"), sat=None, sun=None, out_from=None, out_to=None,
          team="Coordinator", active=True):
    return {"Name": name, "Staff_User": {"id": uid}, "Start_Hour": start, "End_Hour": end, "Days_Off": list(off),
            "Saturday_Hours": sat, "Sunday_Hours": sun, "Out_From": out_from, "Out_To": out_to, "Team": team,
            "Active": active}


# The Team Schedules rows as they stood on 2026-10-19.
TEAM = [sched("Jorge Mier", "u-mier", 12, 21, out_from="2026-10-07"), sched("Alex Morales", "u-morales", 13, 22),
        sched("Oscar Caballero", "u-caballero", off=("Sun", "Mon"), sat="10-17"), sched("Judith Merlo", "u-judith"),
        sched("Ariel Ramirez", "u-ramirez"), sched("Johana Duron", "u-duron", off=("Fri", "Sat"), sun="10-17"),
        sched("Vivian Martinez", "u-vivian", 8, 18, off=("Sun",), team="Biller")]
ACTIVE = {"u-mier", "u-morales", "u-caballero", "u-judith", "u-ramirez", "u-duron", "u-vivian"}
MON_0926 = datetime(2026, 10, 19, 9, 26)       # local; the morning of the 9:26 batch
MON_1400 = datetime(2026, 10, 19, 14, 0)
duron, caballero = TEAM[5], TEAM[2]
ck("weekday hours", pg._hours(duron, "Mon") == (10, 19))
ck("Sunday hours override", pg._hours(duron, "Sun") == (10, 17))
ck("a day off has no hours", pg._hours(duron, "Fri") is None and pg._hours(caballero, "Mon") is None)
ck("out from a date with no end is out", pg._out(TEAM[0], "2026-10-19") and not pg._out(TEAM[0], "2026-10-06"))
ck("Oscar, off Mondays, is next in on Tuesday", pg.next_shift(caballero, MON_1400) == date(2026, 10, 20))
ck("a shift that has ended today moves to the next day",
   pg.next_shift(TEAM[3], datetime(2026, 10, 19, 19, 30)) == date(2026, 10, 20))
load = {"u-judith": 6, "u-ramirez": 3, "u-duron": 4, "u-morales": 3}
ck("before 10 AM: the next one in at 10 with the fewest waiting -- never a disabled user",
   pg.pick_coordinator(TEAM, ACTIVE, load, MON_0926) == ("u-ramirez", "Ariel Ramirez", date(2026, 10, 19)),
   pg.pick_coordinator(TEAM, ACTIVE, load, MON_0926))
ck("at 2 PM: on shift, fewest waiting, then the name (Alex before Ariel)",
   pg.pick_coordinator(TEAM, ACTIVE, load, MON_1400)[0] == "u-morales", pg.pick_coordinator(TEAM, ACTIVE, load, MON_1400))
ck("out (Jorge Mier) and billers are never picked",
   all(pg.pick_coordinator(TEAM, ACTIVE, {**load, "u-mier": 0, "u-vivian": 0}, t)[0] not in ("u-mier", "u-vivian")
       for t in (MON_0926, MON_1400)))
ck("a schedule whose CRM user is no longer active is skipped",
   pg.pick_coordinator([sched("Ana Castro", "u-ana", 8, 17)], ACTIVE, {}, MON_0926) is None)
ck("nobody on the schedules: None", pg.pick_coordinator([], ACTIVE, {}, MON_1400) is None)
NOW_1400 = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)
PRD = deal("p1", stage=PR, paid=True, Scheduling_Owner={"id": "u-ana"}, Retainer_Paid_Date="2026-10-19T09:01:51-04:00",
           Tentative_Procedure_Date="2026-11-18", Deal_Name="Liposuction: Full Front: Nov 18, 2026")
d = pg.decide_owner(PRD, TEAM, ACTIVE, load, MON_1400, NOW_1400)
ck("a disabled coordinator: reassigned, silently", d["result"] == "fix" and d["write"] == {"Scheduling_Owner": {"id": "u-morales"}}
   and d["trigger"] == [] and d["to"] == "u-morales", d)
ck("... with an intro call the 'Intro Call Completed' rule recognises",
   d["task"]["Subject"] == "Intro Call - Pat Example · Deal Nov 18" and d["task"]["Owner"] == {"id": "u-morales"}
   and d["task"]["Due_Date"] == "2026-10-19" and d["task"]["What_Id"] == {"id": "p1"} and d["task"]["$se_module"] == "Deals",
   d["task"])
ck("... and a note that says who and why", "Alex Morales" in d["note"] and "no longer an active" in d["note"], d["note"])
d = pg.decide_owner(dict(PRD, Scheduling_Owner=None, Tentative_Procedure_Date="2026-10-01"), TEAM, ACTIVE, load, MON_1400,
                    NOW_1400)
ck("no coordinator at all: assigned, a past date flagged", d["result"] == "fix" and "is in the past" in d["task"]["Description"]
   and "left it without one" in d["note"], d)
ck("inside 30 minutes: the normal assignment gets its go",
   pg.decide_owner(dict(PRD, Scheduling_Owner=None, Retainer_Paid_Date="2026-10-19T13:45:00-04:00"), TEAM, ACTIVE, load,
                   MON_1400, NOW_1400)["result"] == "wait")
ck("a working coordinator: left alone", pg.decide_owner(dict(PRD, Scheduling_Owner={"id": "u-duron"}), TEAM, ACTIVE, load,
                                                         MON_1400, NOW_1400)["result"] == "ok")
ck("users unreadable: a named coordinator is never second-guessed",
   pg.decide_owner(PRD, TEAM, set(), load, MON_1400, NOW_1400)["result"] == "ok")
ck("not in Payment Received: none of this check's business",
   pg.decide_owner(dict(PRD, Stage="Closed Won - Surgery Scheduled"), TEAM, ACTIVE, load, MON_1400, NOW_1400)["result"] == "skip")
ck("nobody to give it to: a person",
   pg.decide_owner(PRD, [], ACTIVE, load, MON_1400, NOW_1400)["result"] == "review")

# ---- the coordinator's intro text ---------------------------------------------
ck("cities as the patient says them", [pg.city(x) for x in ("Bronx, New York", "bx", "Houston,Texas", "atl", "NEW YORK", "TBD", None)]
   == ["the Bronx", "the Bronx", "Houston", "Atlanta", "New York", "", ""])
ck("surgeon short", pg.surgeon_short("Dr. Phuong X. Nguyen") == "Dr. Nguyen" and pg.surgeon_short(None) == "")
ck("phones: 10 digits, a leading 1 dropped, junk refused",
   pg.phone10("+1 (940) 299-2039") == "9402992039" and pg.phone10("+118323622504") == "" and pg.phone10("", "7734947507") == "7734947507")
t = pg.intro_text("Wanda", "Alex", date(2026, 10, 31), "Dr. Phuong X. Nguyen", "Houston")
ck("the text names the coordinator, date, surgeon and city, and how to stop",
   t.startswith("Hi Wanda, this is Alex from GOALS Plastic Surgery") and "Oct 31 with Dr. Nguyen in Houston." in t
   and t.endswith("Reply STOP to opt out."), t)
ck("a surgeon at an office they never work at: neither is said",
   "Feb 19. Reply" in pg.intro_text("Kahmille", "Johana", date(2027, 2, 19), "Dr. Anthony Perkins", "Atlanta"))
MON_2PM = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)
ID = deal("i1", stage=PR, paid=True, Tentative_Procedure_Date="2026-12-21", Location="Atlanta", Surgeon="Dr. Patrick Narh-Martey",
          RC_SMS_Opt_Out=False)
CON = {"id": "k1", "First_Name": "PAT", "Phone": "(404) 555-0101", "Mobile": None, "Do_Not_Contact": False,
       "ringcentralextensionabr__SMS_OPT_OUT": False}
d = pg.decide_intro(ID, CON, "Oscar", MON_2PM)
ck("a paid patient with a coordinator line: one intro text through SMS Outbox", d["result"] == "send"
   and d["outbox"]["Sender"] == "Oscar" and d["outbox"]["Phone"] == "4045550101" and d["outbox"]["Deal"] == {"id": "i1"}
   and d["outbox"]["Contact"] == {"id": "k1"} and d["outbox"]["Template"] == pg.INTRO_TEMPLATE
   and d["outbox"]["SMS_Text"].startswith("Hi Pat, this is Oscar") and "Dec 21 with Dr. Narh-Martey in Atlanta" in d["outbox"]["SMS_Text"], d)
ck("... and the note on the deal quotes it", d["outbox"]["SMS_Text"] in d["note"])
ck("a coordinator whose line is not set up: held, not settled", pg.decide_intro(ID, CON, None, MON_2PM)["result"] == "hold")
for label, dd, cc in (("deal opted out", dict(ID, RC_SMS_Opt_Out=True), CON),
                      ("contact opted out", ID, dict(CON, ringcentralextensionabr__SMS_OPT_OUT=True)),
                      ("Do Not Contact", ID, dict(CON, Do_Not_Contact=True)), ("no phone", ID, dict(CON, Phone=None))):
    ck("%s: never texted" % label, pg.decide_intro(dd, cc, "Oscar", MON_2PM)["result"] == "skip")
ck("a quoted date already passed: held for a person", pg.decide_intro(dict(ID, Tentative_Procedure_Date="2026-10-01"), CON,
                                                                      "Oscar", MON_2PM)["result"] == "hold")
ck("Sunday: waits", pg.decide_intro(ID, CON, "Oscar", datetime(2026, 10, 18, 18, 0, tzinfo=timezone.utc))["result"] == "wait")
ck("before 9 AM in Atlanta: waits", pg.decide_intro(ID, CON, "Oscar", datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc))["result"] == "wait")
HOU = dict(ID, Location="Houston, Texas", Surgeon="Dr. Phuong X. Nguyen")
ck("9:30 AM in New York is 8:30 in Houston: waits",
   pg.decide_intro(HOU, CON, "Oscar", datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc))["result"] == "wait")
ck("... and goes at 9 in Houston", pg.decide_intro(HOU, CON, "Oscar", datetime(2026, 10, 19, 14, 5, tzinfo=timezone.utc))["result"] == "send")
ck("a contact named Unknown is not greeted by it",
   pg.decide_intro(ID, dict(CON, First_Name="Unknown"), "Oscar", MON_2PM)["outbox"]["SMS_Text"].startswith("Hi, this is Oscar"))
ck("not in Payment Received: none of this", pg.decide_intro(dict(ID, Stage="Closed Won - Surgery Scheduled"), CON, "Oscar",
                                                             MON_2PM)["result"] == "skip")
ck("tagged No Intro Text: never texted", pg.decide_intro(dict(ID, Tag=[{"name": "No Intro Text", "id": "t1"}]), CON, "Oscar",
                                                         MON_2PM)["result"] == "skip")
ck("... nor Do not Call", pg.decide_intro(dict(ID, Tag=[{"name": "Do not Call", "id": "t3"}]), CON, "Oscar",
                                          MON_2PM)["result"] == "skip")
ck("... another tag does not stop it", pg.decide_intro(dict(ID, Tag=[{"name": "VIP", "id": "t2"}]), CON, "Oscar",
                                                       MON_2PM)["result"] == "send")
ck("a quoted date under two weeks out: held for the intro call",
   pg.decide_intro(dict(ID, Tentative_Procedure_Date="2026-10-31"), CON, "Oscar", MON_2PM)["result"] == "hold")
smp = pg.intro_sample(d, "2125550199")
ck("the owner's sample: the same text from the same line, SAMPLE on it, the patient's name out",
   smp["result"] == "send" and smp["deal"] is None and smp["outbox"] == {
       "Name": "Intro sample - Oscar", "Phone": "2125550199", "Sender": "Oscar", "Template": pg.INTRO_SAMPLE_TEMPLATE,
       "SMS_Text": "[SAMPLE] " + d["outbox"]["SMS_Text"].replace("Hi Pat, ", "Hi, ")}, smp)
ck("... on no deal and no contact, and no note", "Deal" not in smp["outbox"] and "Contact" not in smp["outbox"]
   and not smp["note"] and "Pat" not in smp["outbox"]["SMS_Text"])
ck("coordinator off shift: waits for them, not settled", pg.decide_intro(ID, CON, "Oscar", MON_2PM, on_now=False)["result"] == "wait")
judith, mier, alex = TEAM[3], TEAM[0], TEAM[1]
ck("on shift: Judith at 2 PM Monday", pg.on_shift(judith, MON_1400))
ck("... not Oscar on his Monday off, nor Jorge while out", not pg.on_shift(caballero, MON_1400) and not pg.on_shift(mier, MON_1400))
ck("... Alex from 1 PM, not at 9:26", pg.on_shift(alex, MON_1400) and not pg.on_shift(alex, MON_0926))

# ---- the ledger ------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "paid_guard.json")
    st = pg.Store(path)
    ck("armed out of the box", st.data["armed"] is True)
    st.record("pay:1", pg._decision("fix", "x", "d1"), True, "now")
    st.record("pay:2", pg._decision("fix", "x", "d2"), False, "now")
    st.record("pay:3", pg._decision("wait", "grace"), None, "now")
    st.record("pay:4", pg._decision("review", "stage ON HOLD", "d4"), None, "now")
    ck("a fix that landed is settled", st.settled("pay:1") and st.data["seen"]["pay:1"]["result"] == "fixed")
    ck("a failed write is tried again", not st.settled("pay:2") and st.data["seen"]["pay:2"]["result"] == "error")
    ck("a payment in its grace period is looked at again", not st.settled("pay:3"))
    ck("a review is settled and listed", st.settled("pay:4") and "pay:4" in st.data["review"])
    st.record("pay:4", pg._decision("ok", "", "d4"), None, "later")
    ck("an ok later takes it off the review list", "pay:4" not in st.data["review"])
    st.note_run({"label": "auto"})
    st.save()
    st2 = pg.Store(path)
    ck("the ledger survives a restart", st2.data["seen"] == st.data["seen"] and st2.data["runs"] == [{"label": "auto"}])
    with open(path, "w") as f:
        f.write("{broken")
    ck("a broken file is reported, not fatal", "load_error" in pg.Store(path).data)

# ---- the app: wiring and the lookup order ------------------------------------
import app as A  # noqa: E402

ck("the 15-minute loop is one of the worker loops", any(n == "loop:paid-guard" for n, _ in A._WORKER_LOOPS),
   [n for n, _ in A._WORKER_LOOPS])
ck("the ledger lives on the data disk", A._PAID_FILE.name == "paid_guard.json" and A._PAID_FILE.parent == A._DNC_FILE.parent,
   str(A._PAID_FILE))
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
ck("writes need the arm AND the env switch",
   "armed = os.environ.get(\"PAID_GUARD_WRITE\", \"1\") != \"0\" and bool(_paid.data.get(\"armed\"))" in _src
   and "write = armed if write is None else (bool(write) and armed)" in _src, "gates")

_saved = {k: getattr(A, k) for k in ("_paid", "_paid_deals", "_paid_merge_notes", "_paid_handled", "_paid_deal",
                                     "_dnc_put", "_v6_allowed")}
_post = A.requests.post
_headers = A._zoho._headers
calls = {"put": [], "post": []}
try:
    tdir = tempfile.mkdtemp()
    A._paid = pg.Store(os.path.join(tdir, "paid_guard.json"))
    A._paid_handled = lambda d, since: (False, False)
    A._paid_deal = lambda i: dict(BARE) if i == "new1" else None

    # 1. The deal whose Retainer_Invoice_URL names the retainer.
    A._paid_deals = lambda where, page=200: [deal()] if "retainerinvoices/901" in where else []
    A._paid_merge_notes = lambda where, page=200: []
    d = A._paid_payment(pay(), NOW)
    ck("found by its retainer link, then fixed", d["result"] == "fix" and d["deal"] == "d1", d)

    # 2. No deal: the "Merged duplicate:" note that names it.
    A._paid_deals = lambda where, page=200: []
    A._paid_merge_notes = lambda where, page=200: (
        [{"id": "n1", "Parent_Id": {"id": "new1"}, "Created_Time": "2026-10-18T11:00:00-04:00",
          "Note_Content": "FULL RECORD JSON:" + json.dumps(dict(SNAP, Stage="Retainer Invoice Sent", Retainer_Paid=False))}]
        if "retainerinvoices/901" in where else [])
    d = A._paid_payment(pay(), NOW)
    ck("merged away before the payment: the survivor", d["result"] == "fix" and d["deal"] == "new1", d)

    # 3. Neither: a person, shown the Books customer's latest deal.
    A._paid_merge_notes = lambda where, page=200: []
    A._paid_deals = lambda where, page=200: ([deal("d7", Modified_Time="2026-10-01T00:00:00-04:00"),
                                              deal("d8", Modified_Time="2026-10-10T00:00:00-04:00")]
                                             if "Zoho_Books_Customer_ID = '3136880000161627632'" in where else [])
    d = A._paid_payment(pay(), NOW)
    ck("found nowhere: review, with the newest deal of that customer", d["result"] == "review" and d["deal"] == "d8", d)

    # A same-name Journey holds it.
    A._paid_deals = lambda where, page=200: [deal()] if "retainerinvoices/901" in where else []
    A._paid_handled = lambda d, since: (False, True)
    ck("a same-name Journey holds the move", A._paid_payment(pay(), NOW)["result"] == "review")

    # Writing: the deal PUT with the decision's trigger, then the note.
    A._dnc_put = lambda module, rows, trigger: (calls["put"].append((module, rows, trigger)) or
                                                [(rows[0]["id"], True, "SUCCESS")])
    A.requests.post = lambda url, **kw: calls["post"].append((url, kw.get("json"))) or None
    A._zoho._headers = lambda: {"Authorization": "Zoho-oauthtoken test"}
    tot = {"summary": {}, "errors": [], "decisions": []}
    dec = pg.decide_payment(pay(), deal(), False, NOW)
    A._paid_apply("pay:p1", dec, False, "now", tot)
    ck("a dry run writes nothing and records nothing", not calls["put"] and not calls["post"]
       and not A._paid.data["seen"] and tot["summary"] == {"fix": 1}, (calls, tot))
    ck("... and lists field names, never values", tot["decisions"][0]["fields"] == ["Retainer_Paid", "Retainer_Paid_Date",
                                                                                    "Stage"], tot["decisions"])
    A._paid_apply("pay:p1", dec, True, "now", tot)
    ck("a write sends the deal with workflows", calls["put"] == [("Deals", [dict(dec["write"], id="d1")], ["workflow"])],
       calls["put"])
    ck("... leaves a note on the deal", len(calls["post"]) == 1 and calls["post"][0][0].endswith("/crm/v6/Deals/d1/Notes")
       and calls["post"][0][1]["data"][0]["Note_Content"] == dec["note"], calls["post"])
    ck("... and settles it", A._paid.settled("pay:p1") and tot["summary"].get("fixed") == 1, tot["summary"])
    A._dnc_put = lambda module, rows, trigger: [(rows[0]["id"], False, "INVALID_DATA")]
    A._paid_apply("pay:p2", dec, True, "now", tot)
    ck("a refused write is an error, tried again next run, no note",
       not A._paid.settled("pay:p2") and len(calls["post"]) == 1 and tot["errors"][-1]["error"] == "INVALID_DATA", tot)

    # A coordinator fix: the deal, then the intro call task, then the note.
    A._dnc_put = lambda module, rows, trigger: (calls["put"].append((module, rows, trigger)) or
                                                [(rows[0]["id"], True, "SUCCESS")])

    class _R:
        ok, status_code = True, 201
    A.requests.post = lambda url, **kw: calls["post"].append((url, kw.get("json"))) or _R()
    calls["put"].clear(), calls["post"].clear()
    own = pg.decide_owner(PRD, TEAM, ACTIVE, load, MON_1400, NOW_1400)
    A._paid_apply("owner:p1", own, True, "now", tot)
    ck("a coordinator fix writes the deal without workflows",
       calls["put"] == [("Deals", [{"Scheduling_Owner": {"id": "u-morales"}, "id": "p1"}], [])], calls["put"])
    ck("... creates the intro call task, then the note",
       [u.rsplit("/", 2)[-2:] for u, _ in calls["post"]] == [["v6", "Tasks"], ["p1", "Notes"]]
       and calls["post"][0][1] == {"data": [own["task"]], "trigger": []}, calls["post"])
    ck("... and settles it", A._paid.settled("owner:p1"))

    # The run step: which deals it looks at, and that an unreadable user list protects named coordinators.
    _saved_users, _saved_coql = A._zoho.list_users, A._dnc_coql_all
    try:
        A._paid = pg.Store(os.path.join(tdir, "paid_guard2.json"))
        A._paid_deals = lambda where, page=200: [
            dict(PRD, id="a1"), dict(PRD, id="a2", Scheduling_Owner={"id": "u-duron"}),
            dict(PRD, id="a3", Scheduling_Owner=None)] if "Payment Received" in where else []
        A._dnc_coql_all = lambda q, page=2000: TEAM if "Team_Schedules" in q else []
        A._zoho.list_users = lambda: {u: u for u in ACTIVE}
        t2 = {"checked": {}, "summary": {}, "errors": [], "decisions": []}
        A._paid_owners(NOW_1400, 240, False, "now", t2)
        ck("the step finds the disabled one and the empty one, not the working one",
           t2["checked"]["no_coordinator"] == 2 and sorted(x["deal"] for x in t2["decisions"]) == ["a1", "a3"], t2)
        A._zoho.list_users = lambda: {}
        t3 = {"checked": {}, "summary": {}, "errors": [], "decisions": []}
        A._paid_owners(NOW_1400, 240, False, "now", t3)
        ck("users unreadable: only the deal with no coordinator at all", t3["checked"]["no_coordinator"] == 1
           and [x["deal"] for x in t3["decisions"]] == ["a3"], t3)
    finally:
        A._zoho.list_users, A._dnc_coql_all = _saved_users, _saved_coql
    ck("the step runs every pass", '("no_coordinator", lambda: _paid_owners(now, tz_off, write, now_iso, tot))' in _src)

    # Intro texts: off until the live lines are named; then once per deal, through SMS Outbox with workflows.
    _env = os.environ.pop("PAID_GUARD_INTRO_SENDERS", None)
    os.environ.pop("PAID_GUARD_INTRO_SAMPLE_TO", None)
    _saved_coql2 = A._dnc_coql_all
    try:
        asked = []
        A._dnc_coql_all = lambda q, page=2000: asked.append(q) or []
        ck("no senders named: no intro texts and not even a query", A._paid_intro_senders(MON_1400) == {} and not asked)
        os.environ["PAID_GUARD_INTRO_SENDERS"] = "Oscar, Judith, Jorge"
        A._dnc_coql_all = lambda q, page=2000: (
            [dict(caballero, SMS_Sender_Name="Oscar"), dict(judith, SMS_Sender_Name="Judith"),
             dict(duron, SMS_Sender_Name="Johana"), dict(mier, SMS_Sender_Name="Jorge", Active=False)]
            if "Team_Schedules" in q else [CON] if "from Contacts" in q else [])
        ck("only the named lines of active rows, with who is on shift",
           A._paid_intro_senders(MON_1400) == {"u-caballero": ("Oscar", False), "u-judith": ("Judith", True)},
           A._paid_intro_senders(MON_1400))
        A._paid = pg.Store(os.path.join(tdir, "paid_guard3.json"))
        A._paid_deals = lambda where, page=200: [dict(ID, Scheduling_Owner={"id": "u-judith"}),
                                                 dict(ID, id="i2", Scheduling_Owner={"id": "u-duron"}),
                                                 dict(ID, id="i3", Scheduling_Owner={"id": "u-caballero"})]
        calls["post"].clear()

        class _OK:
            ok, status_code, content = True, 201, b"x"

            def json(self):
                return {"data": [{"code": "SUCCESS"}]}
        A.requests.post = lambda url, **kw: calls["post"].append((url, kw.get("json"))) or _OK()
        t4 = {"checked": {}, "summary": {}, "errors": [], "decisions": []}
        A._paid_intros(MON_2PM, 240, True, "now", t4)
        ck("one text, for the coordinator whose line is live and who is on shift", t4["checked"]["intro_pending"] == 2
           and len(calls["post"]) == 2
           and calls["post"][0][0].endswith("/crm/v6/SMS_Outbox") and calls["post"][0][1]["trigger"] == ["workflow"]
           and calls["post"][0][1]["data"][0]["Sender"] == "Judith", (t4, calls["post"][:1]))
        ck("... a note on the deal, and it is settled", calls["post"][1][0].endswith("/crm/v6/Deals/i1/Notes")
           and A._paid.settled("intro:i1") and t4["summary"].get("sent") == 1, t4)
        ck("... Oscar's patient waits for his shift", not A._paid.settled("intro:i3"), A._paid.data["seen"].get("intro:i3"))
        calls["post"].clear()
        A._paid_intros(MON_2PM, 240, True, "now", t4)
        ck("never twice", not calls["post"] and t4["checked"]["intro_pending"] == 1, calls["post"])

        # The owner's sample: once a line, once a day, before its first patient text.
        os.environ["PAID_GUARD_INTRO_SAMPLE_TO"] = "(212) 555-0199"
        A._paid = pg.Store(os.path.join(tdir, "paid_guard4.json"))
        A._paid_deals = lambda where, page=200: [dict(ID, Scheduling_Owner={"id": "u-judith"}),
                                                 dict(ID, id="i4", Scheduling_Owner={"id": "u-judith"}),
                                                 dict(ID, id="i3", Scheduling_Owner={"id": "u-caballero"})]
        calls["post"].clear()
        t5 = {"checked": {}, "summary": {}, "errors": [], "decisions": []}
        A._paid_intros(MON_2PM, 240, True, "now", t5)
        out = [(u.rsplit("/", 1)[-1] if "Notes" not in u else "note", (j or {}).get("data", [{}])[0]) for u, j in calls["post"]]
        ck("the sample goes first, from Judith's line to the owner, then her two patients",
           [(k, r.get("Phone"), r.get("Template")) for k, r in out if k == "SMS_Outbox"]
           == [("SMS_Outbox", "2125550199", pg.INTRO_SAMPLE_TEMPLATE), ("SMS_Outbox", "4045550101", pg.INTRO_TEMPLATE),
               ("SMS_Outbox", "4045550101", pg.INTRO_TEMPLATE)] and out[0][1]["Sender"] == "Judith"
           and out[0][1]["SMS_Text"].startswith("[SAMPLE] Hi, this is Judith"), out)
        ck("... no sample for Oscar, who sent nothing", sum(1 for k, r in out if r.get("Template") == pg.INTRO_SAMPLE_TEMPLATE) == 1)
        ck("... settled for the day, by line", A._paid.settled("intro-sample:Judith:2026-10-19")
           and not A._paid.settled("intro-sample:Oscar:2026-10-19"), sorted(A._paid.data["seen"]))
        A._paid_deals = lambda where, page=200: [dict(ID, id="i5", Scheduling_Owner={"id": "u-judith"})]
        calls["post"].clear()
        A._paid_intros(MON_2PM, 240, True, "now", t5)
        ck("... and not again that day", [(j or {}).get("data", [{}])[0].get("Template") for u, j in calls["post"]
                                           if u.endswith("SMS_Outbox")] == [pg.INTRO_TEMPLATE], calls["post"])

        class _NO:
            ok, status_code, content = False, 400, b"x"

            def json(self):
                return {"data": [{"code": "INVALID_DATA"}]}
        A._paid = pg.Store(os.path.join(tdir, "paid_guard5.json"))
        calls["post"].clear()
        A.requests.post = lambda url, **kw: calls["post"].append((url, kw.get("json"))) or _NO()
        t6 = {"checked": {}, "summary": {}, "errors": [], "decisions": []}
        A._paid_intros(MON_2PM, 240, True, "now", t6)
        ck("a sample that does not go: no patient text from that line, both looked at again",
           len(calls["post"]) == 1 and not A._paid.settled("intro:i5") and not A._paid.settled("intro-sample:Judith:2026-10-19")
           and A._paid.data["seen"]["intro:i5"]["reason"] == "the sample to the owner did not go", (calls["post"], t6))
        A.requests.post = lambda url, **kw: calls["post"].append((url, kw.get("json"))) or _OK()
        calls["post"].clear()
        t7 = {"checked": {}, "summary": {}, "errors": [], "decisions": []}
        A._paid_intros(MON_2PM, 240, False, "now", t7)
        ck("a dry pass sends nothing, and shows the sample and the text it would send",
           not calls["post"] and [x["reason"] for x in t7["decisions"]] == ["intro sample to the owner", "intro text"], t7)
    finally:
        A._dnc_coql_all = _saved_coql2
        os.environ.pop("PAID_GUARD_INTRO_SENDERS", None)
        os.environ.pop("PAID_GUARD_INTRO_SAMPLE_TO", None)
        if _env is not None:
            os.environ["PAID_GUARD_INTRO_SENDERS"] = _env
    ck("the intro step runs every pass", '("intro_texts", lambda: _paid_intros(now, tz_off, write, now_iso, tot))' in _src)
    ck("Ana Castro is off the shift table", "Ana Castro" not in A.SHIFTS, sorted(A.SHIFTS))

    A._v6_allowed = lambda: False
    r401 = A.app.test_client().get("/api/v6/paid-guard")
    ck("the route is signed-in only", r401.status_code == 401, r401.status_code)
finally:
    for k, v in _saved.items():
        setattr(A, k, v)
    A.requests.post = _post
    A._zoho._headers = _headers

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
