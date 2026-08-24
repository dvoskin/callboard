"""Regression: talk time per RingCX channel, in the expand panel.

Two things RingCX does that a naive grouping gets wrong.

1. An agent's OWN queue is named after the agent -- "Charlotte McKay" appears as
   a Channel alongside "ENG - Manual Upload" and "v2 ENG - New Lead - Scheduled".
   Shown raw it reads as if she ran a campaign named after herself, so it is
   relabelled "Personal queue".

2. parse_interaction_csv deliberately blanks campaign_name on UC rows, so
   grouping on campaign_name would drop an agent's own line out of their own
   breakdown -- exactly the rows this feature exists to surface. The raw Channel
   is carried separately for that reason.

Also: the breakdown must ACCOUNT for all the talk it is a breakdown of. A tail
rolled into "N more" still carries its seconds, so the lines sum to the row total.
"""
import sys

sys.path.insert(0, __file__.rsplit('/', 1)[0])
from v5_report import build_report, CHANNELS_SHOWN

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:220]) if not cond else ""))
    if not cond: fail.append(name)

def cx(agent, channel, talk, i=0, uc=False):
    return {"agent_name": agent, "talk_time": talk, "duration": talk,
            "direction": "Outbound", "result": "Connected",
            "start_time": "08/21/2026 09:%02d:00" % (i % 60),
            "ani": "+1555000%04d" % i, "dnis": "+1555111%04d" % i,
            "campaign_name": "" if uc else channel,
            "queue_name": "UC" if uc else "",
            "channel": channel,
            "call_type": "UC Call" if uc else "Voice",
            "agent_disposition": "", "wrap_time": 0, "source": "ringcx_csv"}

W = {"start": "2026-08-21", "end": "2026-08-21"}

rows = (
    [cx("Charlotte McKay", "ENG - Manual Upload", 100, i) for i in range(3)]        # 300
    + [cx("Charlotte McKay", "v2 ENG - New Lead - Scheduled", 200, 10 + i) for i in range(2)]  # 400
    + [cx("Charlotte McKay", "Charlotte McKay", 50, 20 + i) for i in range(2)]      # 100 personal
    + [cx("Charlotte McKay", "UC", 30, 30, uc=True)]                                 # 30 own line
)
r = build_report([], rows, window=W, roster={"charlotte mckay"})
a = (r["ranked"] + r["unranked"])[0]
ch = {c["name"]: c["talk"] for c in a["channels"]}

ok("channels are present on the row", bool(a.get("channels")), a.keys())
ok("a campaign channel is kept by name", ch.get("ENG - Manual Upload") == 300, ch)
ok("the busiest channel is listed first",
   a["channels"][0]["name"] == "v2 ENG - New Lead - Scheduled", a["channels"])
ok("the agent's own queue is relabelled", ch.get("Personal queue") == 100, ch)
ok("her name is not shown as a campaign", "Charlotte McKay" not in ch, ch)
ok("the UC row survives -- campaign_name is blank there", ch.get("UC") == 30, ch)
ok("the breakdown accounts for all the talk",
   sum(c["talk"] for c in a["channels"]) == a["talk"],
   (sum(c["talk"] for c in a["channels"]), a["talk"]))

# the long tail is rolled up, and still carries its seconds
many = [cx("Reidy Rosello", "Queue %d" % i, 10 * (i + 1), 100 + i)
        for i in range(CHANNELS_SHOWN + 4)]
r2 = build_report([], many, window=W, roster={"reidy rosello"})
a2 = (r2["ranked"] + r2["unranked"])[0]
ok("the list is capped", len(a2["channels"]) == CHANNELS_SHOWN + 1, len(a2["channels"]))
ok("the tail is labelled with its own count",
   a2["channels"][-1]["name"] == "4 more", a2["channels"][-1])
ok("the rolled-up tail keeps its seconds",
   sum(c["talk"] for c in a2["channels"]) == a2["talk"],
   (sum(c["talk"] for c in a2["channels"]), a2["talk"]))

# the panel renders it
import io
tpl = io.open("templates/scoreboard_v5.html", encoding="utf-8").read()
ok("the panel renders the breakdown", "a.channels && a.channels.length" in tpl)
ok("it is full width in the panel grid", ".chan{grid-column:1/-1" in tpl)

# ---- through the REAL parser, not hand-built rows --------------------------
# The rows above are constructed directly, so they never prove that
# parse_interaction_csv carries the Channel. It blanks campaign_name on UC rows
# by design, and reusing that field is the mistake this guards -- an agent's own
# line would vanish from their own breakdown.
from v5_report import parse_interaction_csv

HDR = ("Date,Agent Full Name,Interaction Start Time,Channel Type,Channel,"
       "Call Type,Call Result,Talk Time (min),Sum of Interaction Duration,"
       "Lead Phone,Caller ID,Agent Disposition,Wrap Time (min)")
CSV = "\n".join([
    HDR,
    "08/21/2026,Charlotte McKay,09:00:00 ,Voice,ENG - Manual Upload,Outbound,Connected,120,120,'+15551112222,'+15553334444,Interested,10",
    "08/21/2026,Charlotte McKay,09:10:00 ,Voice,Charlotte McKay,Outbound,Connected,60,60,'+15551112223,'+15553334445,Interested,5",
    "08/21/2026,Charlotte McKay,09:20:00 ,UC Call,UC,Outbound,Connected,30,30,'+15551112224,'+15553334446,Interested,0",
]) + "\n"
parsed, unit = parse_interaction_csv(CSV)
chans = [r.get("channel") for r in parsed]
ok("the parser keeps Channel on a campaign row", "ENG - Manual Upload" in chans, chans)
ok("the parser keeps Channel on the agent's own queue", "Charlotte McKay" in chans, chans)
ok("the parser keeps Channel on a UC row -- where campaign_name is blank",
   "UC" in chans, chans)
uc_row = [r for r in parsed if r.get("call_type") == "UC Call"][0]
ok("that UC row really does have a blank campaign_name",
   uc_row.get("campaign_name") == "" and uc_row.get("channel") == "UC", uc_row)

r3 = build_report([], parsed, window=W, roster={"charlotte mckay"})
a3 = (r3["ranked"] + r3["unranked"])[0]
names3 = [c["name"] for c in a3["channels"]]
ok("end to end, all three channels reach the row",
   "ENG - Manual Upload" in names3 and "Personal queue" in names3 and "UC" in names3,
   names3)

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
