"""Inbound vs outbound on the sales board (/v5).

Danny, 2026-10-15: "show inbound vs outbound calls on the sales call tracker".
Run: ./.venv/bin/python -B checks_v5_direction.py   (tally LAST)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v5_report as V  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


# ---- the classifier: the words each source writes, and never a guess ----
for raw, want in (("Inbound", "in"), ("INBOUND", "in"), ("Incoming", "in"), ("inbound queue", "in"),
                  ("Outbound", "out"), ("OUTBOUND", "out"), ("Manual", "out"), ("MANUAL OUTBOUND", "out"),
                  ("Preview", "out"), ("Predictive", "out"), ("Outgoing", "out"),
                  ("", "unknown"), ("Voice", "unknown"), ("UC Call", "unknown"), ("Transfer", "unknown"), (None, "unknown")):
    ck("direction %r -> %s" % (raw, want), V.classify_direction(raw) == want, V.classify_direction(raw))

# the channel speaks only when the row's own field is silent
ck("an _IB queue with no direction is inbound", V._row_direction({"direction": "", "channel": "New_Bilingual_IB"}) == "in", "ib")
ck("a New Lead campaign with no direction is outbound", V._row_direction({"direction": "Voice", "channel": "v2 ENG - New Lead - Scheduled"}) == "out", "nl")
ck("the row's own field beats the channel", V._row_direction({"direction": "Outbound", "channel": "New_Bilingual_IB"}) == "out", "own field")
ck("an unknown channel and no direction stays unknown", V._row_direction({"direction": "", "channel": "Manual Upload"}) == "unknown", "unknown")

# ---- the report: per rep, totals, a warning for what could not be read ----
cx = [
    # campaign rows (dialer and inbound queue), Interaction Report shape
    {"agent_name": "Roman Gomez", "direction": "Outbound", "result": "Outbound Answered", "talk_time": 200, "duration": 220,
     "start_time": "2026-10-15 10:00:00", "campaign_name": "v2 ENG - New Lead - Call Now", "channel": "v2 ENG - New Lead - Call Now", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "Outbound", "result": "No Answer", "talk_time": 0, "duration": 30,
     "start_time": "2026-10-15 10:05:00", "campaign_name": "v2 ENG - New Lead - Call Now", "channel": "v2 ENG - New Lead - Call Now", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "Inbound", "result": "Inbound Accepted", "talk_time": 400, "duration": 420,
     "start_time": "2026-10-15 11:00:00", "campaign_name": "New_Bilingual_IB", "channel": "New_Bilingual_IB", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "", "result": "Inbound Missed", "talk_time": 0, "duration": 20,
     "start_time": "2026-10-15 11:30:00", "campaign_name": "New_Bilingual_IB", "channel": "New_Bilingual_IB", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "Voice", "result": "Call Connected", "talk_time": 60, "duration": 70,
     "start_time": "2026-10-15 12:00:00", "campaign_name": "Manual Upload", "channel": "Manual Upload", "queue_name": "", "call_type": "Voice"},
]
ex = [  # a RingEX direct call nobody in RingCX matched
    {"agent_name": "Roman Gomez", "direction": "Inbound", "result": "Accepted", "duration": 90, "start_time": "2026-10-15T15:00:00Z",
     "from_number": "+13055550101", "to_number": "+12125550100"},
]
rep = V.build_report(ex, cx, tz_offset_minutes=-240, window={"start": "2026-10-15", "end": "2026-10-15"})
ag = next(a for a in rep["ranked"] + rep["unranked"] if a["name"] == "Roman Gomez")
ck("outbound counts the dialer rows", ag["outbound"] == 2 and ag["outbound_connected"] == 1, ag)
ck("inbound counts the queue rows (own field AND channel fallback) and the RingEX direct call",
   ag["inbound"] == 3 and ag["inbound_answered"] == 2, ag)
ck("a row with no readable direction is counted in Dials only", ag["dir_unknown"] == 1 and ag["attempts"] == 6
   and ag["inbound"] + ag["outbound"] + ag["dir_unknown"] == ag["attempts"], ag)
ck("the totals carry the split", rep["totals"]["inbound"] == 3 and rep["totals"]["outbound"] == 2 and rep["totals"]["dir_unknown"] == 1
   and rep["totals"]["inbound_answered"] == 2 and rep["totals"]["outbound_connected"] == 1, rep["totals"])
w = [x for x in rep["warnings"] if x["kind"] == "unclassified_direction"]
ck("an unreadable direction is surfaced, with the raw value", w and dict(w[0]["values"]).get("Voice") == 1, rep["warnings"])

# ---- the page (in/out moved from the row to the panel on 2026-10-15) ----
tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v5.html"), encoding="utf-8").read() + \
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "_board_style.html"), encoding="utf-8").read()  # styles moved to the shared partial 2026-10-16
ck("the panel carries inbound with answered, and outbound as a plain count",
   "cell2('Inbound', w(a.inbound || 0) + ' <i>' + w(a.inbound_answered || 0) + ' answered</i>'" in tpl
   and "cell2('Outbound', w(a.outbound || 0), !a.outbound)" in tpl, "panel")
ck("a Calls row in the summary block: inbound with answered, outbound with NO connected count (it only means no error)",
   "<span class=\"sl\">Calls</span>" in tpl and "'</b> inbound <i>' + fmt(TT.inbound_answered) + ' answered</i>'" in tpl
   and "'</b> outbound' +" in tpl and "outbound_connected" not in tpl and "' connected</i>'" not in tpl, "team line")

# ---- design pass, 2026-10-15: "accommodate all the data more efficiently on desktop and mobile"
ck("the board widens on a desktop so a rep's texts fit on one line", "@media(min-width:900px){.wrap{max-width:920px}}" in tpl, "width")
ck("the summary is one labelled block: Floor, Sent, Calls, Texts",
   "h += '<div class=\"sum\"><div class=\"floor srow\">" in tpl and "<div class=\"tot srow\"><span class=\"sl\">Sent</span>" in tpl
   and "id=\"smsrow\" hidden><span class=\"sl\">Texts</span>" in tpl and ".sum .srow{display:grid;grid-template-columns:62px minmax(0,1fr)" in tpl, "summary")   # 62px on every board since 2026-10-20
ck("big totals are written 3,095 not 3095", "function fmt(n) { return Math.round(n || 0).toLocaleString('en-US'); }" in tpl, "fmt")
ck("data notes fold under one line, and the texts table sits above them",
   "<details class=\"notes\" id=\"datanotes\"><summary>Data notes'" in tpl and tpl.count("nNotes++, notes += '") == 5   # the fifth: follow-ups, 2026-10-19
   and "(dn ? dn : $('out')).insertAdjacentHTML(dn ? 'beforebegin' : 'beforeend'" in tpl, "notes")
ck("a stale or stopped report still warns in the open", "if (x.kind === 'report_stale') h += '<div class=\"warn\">'" in tpl, "stale warn")

# ---- missed calls (Danny, 2026-10-15: "add missed calls to sales call tracker") ----
cx2 = [
    # Interaction Report rows: Lead Phone (ani) is the lead both ways
    {"agent_name": "Roman Gomez", "direction": "Inbound", "result": "Inbound Missed", "talk_time": 0, "duration": 10, "source": "ringcx_csv",
     "start_time": "2026-10-15 10:00:00", "ani": "3055550101", "campaign_name": "New_Bilingual_IB", "channel": "New_Bilingual_IB", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Ana Rep", "direction": "Outbound", "result": "Outbound Answered", "talk_time": 60, "duration": 70, "source": "ringcx_csv",
     "start_time": "2026-10-15 10:30:00", "ani": "3055550101", "campaign_name": "v2 ENG - New Lead - Call Now", "channel": "v2 ENG - New Lead - Call Now", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "Inbound", "result": "Inbound Missed", "talk_time": 0, "duration": 10, "source": "ringcx_csv",
     "start_time": "2026-10-15 11:00:00", "ani": "3055550202", "campaign_name": "New_Bilingual_IB", "channel": "New_Bilingual_IB", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "Inbound", "result": "Inbound Missed", "talk_time": 0, "duration": 10, "source": "ringcx_csv",
     "start_time": "2026-10-15 12:00:00", "ani": "3055550303", "campaign_name": "New_Bilingual_IB", "channel": "New_Bilingual_IB", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "Inbound", "result": "Inbound Accepted", "talk_time": 120, "duration": 130, "source": "ringcx_csv",
     "start_time": "2026-10-15 12:20:00", "ani": "3055550303", "campaign_name": "New_Bilingual_IB", "channel": "New_Bilingual_IB", "queue_name": "", "call_type": "Voice"},
    {"agent_name": "Roman Gomez", "direction": "Outbound", "result": "No Answer", "talk_time": 0, "duration": 30, "source": "ringcx_csv",
     "start_time": "2026-10-15 09:00:00", "ani": "3055550404", "campaign_name": "v2 ENG - New Lead - Call Now", "channel": "v2 ENG - New Lead - Call Now", "queue_name": "", "call_type": "Voice"},
]
rep2 = V.build_report([], cx2, tz_offset_minutes=-240, window={"start": "2026-10-15", "end": "2026-10-15"})
rg = next(a for a in rep2["ranked"] + rep2["unranked"] if a["name"] == "Roman Gomez")
byn = {m["number"]: m for m in rg["missed_calls"]}
ck("three missed inbound; an outbound no-answer is not a missed call", rg["missed_in"] == 3 and "(305) 555-0404" not in byn, rg["missed_calls"])
ck("a teammate calling the number back clears it, and says who and when", byn["(305) 555-0101"]["resolved"] and byn["(305) 555-0101"]["back"]["by"] == "Ana Rep"
   and byn["(305) 555-0101"]["back"]["at"] == "10:30 AM" and byn["(305) 555-0101"]["back"]["how"] == "called back", byn["(305) 555-0101"])
ck("the number calling again and being answered clears it", byn["(305) 555-0303"]["resolved"] and byn["(305) 555-0303"]["back"]["how"] == "called again, answered", byn["(305) 555-0303"])
ck("one stays open, newest first, in local time", rg["missed_open"] == 1 and not byn["(305) 555-0202"]["resolved"]
   and [m["at"] for m in rg["missed_calls"]] == ["12:00 PM", "11:00 AM", "10:00 AM"], rg["missed_calls"])
ck("the totals carry missed and still open", rep2["totals"]["missed_in"] == 3 and rep2["totals"]["missed_open"] == 1, rep2["totals"])
ck("RingEX rows: the caller on an inbound, the callee on an outbound", V._party({"from_number": "+13055550101", "to_number": "+12125550100"}, "in") == "3055550101"
   and V._party({"from_number": "+12125550100", "to_number": "+13055550101"}, "out") == "3055550101", "party")

import app as A  # noqa: E402
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
ck("the no-login share link gets counts, never a lead's number",
   "if GOOGLE_CLIENT_ID and not session.get(\"user\") and not _word_authed():" in src and "a_.pop(\"missed_calls\", None)" in src, "share strip")

# ---- the collapsed row: only what needs doing (2026-10-15) ----
ck("the collapsed row is talk, dials, >3m, quotes, paid retainers",
   "grid-template-areas:\"rk nm tk d t3 q rt\"" in tpl and "ioCell(a) +" not in tpl and "cell(a.over_10, f.over_10" not in tpl
   and "\".  d  t3 q  rt . .\"" in tpl, "row")
ck("under the name: missed, unread, waiting -- only when non-zero", "if (a.missed_open) bits.push('<b class=\"hot\">' + a.missed_open + '</b> missed');" in tpl
   and "if (!bits.length) return '';" in tpl and "pressLine(a.name) + '</span>'" in tpl, "press line")
ck("the panel carries in/out, over 10 min, texts and the missed list",
   "cell2('Over 10 min', w(a.over_10 || 0), !a.over_10)" in tpl and "'<span class=\"dsms\" data-name=\"'" in tpl and "'</div>' + missedList(a) + '</div>';" in tpl
   and "'<a class=\"ml-n\" href=\"tel:'" in tpl, "panel")
# ---- retainers: the row shows PAID (Danny: "the actual paid retainer counts are off") ----
ck("the row's retainer column is retainers PAID, sent moves to the panel",
   "bcell(bk.retainers_paid, 'retainers_paid', 'rt', 'paid', 'paid')" in tpl and "bcell(bk.retainers_sent, 'retainers_sent', 'rt'" not in tpl
   and "cell2('Retainers sent', booksHas('retainers_sent')" in tpl and "'</b> paid' : '')" in tpl, "paid column")

# ---- dispositions, carried as written (Danny, 2026-10-15) ----
cx3 = [dict(cx2[0], agent_disposition="Left Voicemail"), dict(cx2[1], agent_disposition="Interested - Quote Sent"),
       dict(cx2[2], agent_disposition="Left Voicemail"), dict(cx2[3], agent_disposition="")]
rep3 = V.build_report([], cx3, tz_offset_minutes=-240, window={"start": "2026-10-15", "end": "2026-10-15"})
rg3 = next(a for a in rep3["ranked"] + rep3["unranked"] if a["name"] == "Roman Gomez")
ck("each rep carries their dispositions with direction and count, blanks left out",
   rg3["dispositions"] == [["Left Voicemail", "in", 2]], rg3["dispositions"])
ck("the floor-wide tally sums every rep", ["Left Voicemail", "in", 2] in rep3["dispositions"] and ["Interested - Quote Sent", "out", 1] in rep3["dispositions"], rep3["dispositions"])

# ---- outcomes from what the rep recorded (instead of "connected") ----
for raw, want in (("No Answer", "not_reached"), ("Voicemail / No Answer", "not_reached"), ("Answering Machine", "not_reached"),
                  ("Bad Connection/Don't Hear", "not_reached"), ("Wrong Number", "not_reached"), ("Call dropped / Audio Issues", "not_reached"),
                  ("[No-Agent-Disp]", "none"), ("Disposition Timeout", "none"), ("DNC (Do Not Call)", "dnc"),
                  ("Follow Up Scheduled", "booked"), ("Call Scheduled", "booked"), ("Quote Sent", "quoted"), ("Quoted", "quoted"),
                  ("General Questions", "reached"), ("Price Shopping/Not Ready", "reached"), ("", "")):
    ck("disposition %r -> %s" % (raw, want), V.classify_disposition(raw) == want, V.classify_disposition(raw))
cx4 = [dict(cx2[1], agent_name="Roman Gomez", agent_disposition=d, start_time="2026-10-15 1%d:00:00" % i)
       for i, d in enumerate(["No Answer", "Voicemail", "Follow Up Scheduled", "Quote Sent", "General Questions", "DNC (Do Not Call)", "[No-Agent-Disp]"])]
rep4 = V.build_report([], cx4, tz_offset_minutes=-240, window={"start": "2026-10-15", "end": "2026-10-15"})
rg4 = next(a for a in rep4["ranked"] + rep4["unranked"] if a["name"] == "Roman Gomez")
ck("reached = booked + quoted + other talk; a do-not-call, no answer and no disposition are not",
   (rg4["reached"], rg4["booked"], rg4["quoted"], rg4["dnc"], rg4["not_reached"], rg4["no_disposition"]) == (3, 1, 1, 1, 2, 1), rg4)
ck("do-not-call has its own red count on the summary", "fmt(TT.dnc) + '</b> do not call'" in tpl, "dnc summary")
ck("the summary and the panel show reached and booked; no-disposition and DNC only when there are any",
   "</b> reached <i>' + fmt(TT.booked) + ' next step booked</i>'" in tpl and "cell2('Reached', w(a.reached || 0)" in tpl
   and "(a.no_disposition ? cell2('No disposition'" in tpl and "(a.dnc ? cell2('Do not call'" in tpl, "outcomes ui")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
