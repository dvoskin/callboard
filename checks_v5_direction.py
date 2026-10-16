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

# ---- the page ----
tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scoreboard_v5.html"), encoding="utf-8").read()
ck("a column after Dials on desktop; on a phone the call figures get their own row",
   "grid-template-areas:\"rk nm tk d io t3 t10 q rt\"" in tpl and "<span class=\"a-io\"" in tpl
   and "\".  d  io t3 t10 . .\" \".  q  rt .  .   . .\"" in tpl, "grid")
ck("each row shows in . out with the answered/connected counts on hover",
   "cell(a.attempts, f.attempts, 'd', ' dials') +\n           ioCell(a) +" in tpl and "' inbound (' + (a.inbound_answered || 0) + ' answered)" in tpl, "row")
ck("the panel carries inbound answered and outbound connected",
   "cell2('Inbound', w(a.inbound || 0) + ' <i>' + w(a.inbound_answered || 0) + ' answered</i>'" in tpl
   and "cell2('Outbound', w(a.outbound || 0) + ' <i>' + w(a.outbound_connected || 0) + ' connected</i>'" in tpl, "panel")
ck("a Calls row in the summary block, with answered and connected",
   "<span class=\"sl\">Calls</span>" in tpl and "'</b> inbound <i>' + fmt(TT.inbound_answered) + ' answered</i>'" in tpl
   and "'</b> outbound <i>' + fmt(TT.outbound_connected) + ' connected</i>'" in tpl, "team line")

# ---- design pass, 2026-10-15: "accommodate all the data more efficiently on desktop and mobile"
ck("the board widens on a desktop so a rep's texts fit on one line", "@media(min-width:900px){.wrap{max-width:920px}}" in tpl, "width")
ck("the summary is one labelled block: Floor, Sent, Calls, Texts",
   "h += '<div class=\"sum\"><div class=\"floor srow\">" in tpl and "<div class=\"tot srow\"><span class=\"sl\">Sent</span>" in tpl
   and "id=\"smsrow\" hidden><span class=\"sl\">Texts</span>" in tpl and ".sum .srow{display:grid;grid-template-columns:44px minmax(0,1fr)" in tpl, "summary")
ck("big totals are written 3,095 not 3095", "function fmt(n) { return Math.round(n || 0).toLocaleString('en-US'); }" in tpl, "fmt")
ck("data notes fold under one line, and the texts table sits above them",
   "<details class=\"notes\" id=\"datanotes\"><summary>Data notes'" in tpl and tpl.count("nNotes++, notes += '") == 4
   and "(dn ? dn : $('out')).insertAdjacentHTML(dn ? 'beforebegin' : 'beforeend'" in tpl, "notes")
ck("a stale or stopped report still warns in the open", "if (x.kind === 'report_stale') h += '<div class=\"warn\">'" in tpl, "stale warn")

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
