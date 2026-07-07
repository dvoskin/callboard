"""Parse a RingCX "Interactions" CSV export into per-agent call stats.

Self-contained (stdlib only) so it works without any live RingCX/Zoho access —
the /v2/interactions page uploads an export and this turns it into the same
stats we'd otherwise pull from the (WEM-gated, undercounting) live API.

Data quirks handled (verified against the 7-day export, 2026-07):
  * Duration columns are labeled "(min)" but hold SECONDS.
  * Talk Time is blank on ~75% of connected outbound legs, so Interaction Time
    (populated on every connected call, equal to talk time when both exist) is
    the duration measure.
  * A trailing "Average" summary row and a UTF-8 BOM on the header are dropped.
  * "Outbound Answered" includes carrier/voicemail pickups — only calls with a
    real outcome disposition count as conversations.
  * Dial source comes from the Channel field: campaigns use an en-dash name
    ("ENG – Hot Deal – Call Now"); personal-queue dials carry the agent's name.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict

CONNECTED_RESULT = "Outbound Answered"
NON_CONVERSATION_DISPS = {
    "No Answer", "Voicemail", "Voicemail / No Answer", "No Answer / Voicemail",
    "Answering Machine", "[No-Agent-Disp]", "Disposition Timeout", "No Disposition",
}
REQUIRED_COLUMNS = {"Agent Full Name", "Call Type", "Channel", "Interaction Time (min)"}


class ReportError(ValueError):
    """Raised when the uploaded file isn't a recognizable interactions export."""


def _fsec(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def dial_source(channel: str) -> str:
    """Classify a RingCX dialer outbound dial by its Channel (queue) name.

    RingEX (UC) calls are handled by the caller before this runs, so they never
    reach here.
    """
    if "_IB" in channel or channel.startswith("DDR") or channel.startswith("New_"):
        return "inbound_queue"
    if "–" in channel:            # en-dash → dialer campaign
        return "campaign"
    if channel in ("N/A", ""):
        return "unknown"
    return "personal"                  # channel == agent name


def analyze_csv(text: str) -> dict:
    """Parse CSV text and return a JSON-serializable stats dict.

    Raises ReportError if the file doesn't look like an interactions export.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        raise ReportError(
            "This doesn't look like a RingCX Interactions export — missing "
            + ", ".join(sorted(missing)) + "."
        )

    agents: dict[str, dict] = defaultdict(lambda: {
        "outbound": 0, "connected": 0, "convos": 0, "ob_secs": 0.0,
        "over3": 0, "over10": 0, "longest": 0.0, "wrap_secs": 0.0,
        "camp": 0, "camp_secs": 0.0, "pers": 0, "pers_secs": 0.0,
        "inbound": 0, "ib_secs": 0.0, "ringex": 0, "ringex_secs": 0.0,
        "days": set(), "disps": Counter(),
    })
    daily: dict[str, Counter] = defaultdict(Counter)
    channels: dict[str, Counter] = defaultdict(Counter)
    all_disps: Counter = Counter()
    src_totals: Counter = Counter()
    src_secs: Counter = Counter()
    dates: set[str] = set()
    total_rows = 0

    for r in reader:
        date = (r.get("Date") or "").strip()
        if not date or date == "Average":       # trailing summary row
            continue
        total_rows += 1
        name = (r.get("Agent Full Name") or "").strip() or "(unknown)"
        a = agents[name]
        ctype = r.get("Call Type", "")
        chan = r.get("Channel", "")
        chan_type = r.get("Channel Type", "")
        disp = r.get("Agent Disposition", "")
        result = r.get("Call Result", "")
        dur = _fsec(r.get("Interaction Time (min)"))
        a["days"].add(date)
        dates.add(date)
        d = daily[date]

        # UC calls are ordinary calls placed/received on RingEX (not the RingCX
        # dialer). Count them as normal in/outbound activity, just flag the
        # platform so it's visible how much rode RingEX.
        on_ringex = chan_type in ("UC Call", "UC Meeting") or chan == "UC"
        if on_ringex:
            a["ringex"] += 1
            a["ringex_secs"] += dur
            d["ringex"] += 1

        if ctype == "OUTBOUND":
            a["outbound"] += 1
            a["ob_secs"] += dur
            a["wrap_secs"] += _fsec(r.get("Wrap Time (min)"))
            a["disps"][disp] += 1
            all_disps[disp] += 1
            a["longest"] = max(a["longest"], dur)
            d["outbound"] += 1
            d["ob_secs"] += dur

            # RingEX dials aren't dialer-queue calls, so they sit outside the
            # campaign/personal split.
            src = "ringex" if on_ringex else dial_source(chan)
            src_totals[src] += 1
            src_secs[src] += dur
            if src == "campaign":
                a["camp"] += 1
                a["camp_secs"] += dur
            elif src == "personal":
                a["pers"] += 1
                a["pers_secs"] += dur

            cc = channels[chan]
            cc["n"] += 1
            cc["secs"] += dur
            cc["_src_" + src] = 1
            # RingEX outbound legs report "UC Call" (not "Outbound Answered"), so
            # treat any RingEX dial with talk time as connected.
            connected = result == CONNECTED_RESULT or (on_ringex and dur > 0)
            if connected:
                a["connected"] += 1
                d["connected"] += 1
                cc["conn"] += 1
                if disp not in NON_CONVERSATION_DISPS:
                    a["convos"] += 1
            if dur > 180:
                a["over3"] += 1
                d["over3"] += 1
            if dur > 600:
                a["over10"] += 1
                d["over10"] += 1
        elif ctype == "INBOUND":
            a["inbound"] += 1
            a["ib_secs"] += dur
            d["inbound"] += 1

    if total_rows == 0:
        raise ReportError("The file parsed but contained no interaction rows.")

    # ---- shape the response ----
    def agent_row(name, a):
        conn = a["connected"]
        return {
            "name": name,
            "days": len(a["days"]),
            "outbound": a["outbound"],
            "connected": conn,
            "connect_pct": round(100.0 * conn / a["outbound"], 1) if a["outbound"] else 0.0,
            "convos": a["convos"],
            "over3": a["over3"],
            "over10": a["over10"],
            "talk_secs": round(a["ob_secs"]),
            "avg_secs": round(a["ob_secs"] / conn) if conn else 0,
            "longest_secs": round(a["longest"]),
            "camp": a["camp"], "camp_secs": round(a["camp_secs"]),
            "pers": a["pers"], "pers_secs": round(a["pers_secs"]),
            "wrap_secs": round(a["wrap_secs"]),
            "inbound": a["inbound"], "ib_secs": round(a["ib_secs"]),
            "ringex": a["ringex"], "ringex_secs": round(a["ringex_secs"]),
            "nocontact": sum(v for k, v in a["disps"].items() if k in NON_CONVERSATION_DISPS
                             and k not in ("[No-Agent-Disp]", "Disposition Timeout", "No Disposition")),
            "nodisp": sum(v for k, v in a["disps"].items()
                          if k in ("[No-Agent-Disp]", "Disposition Timeout", "No Disposition")),
            "top_disps": [[k, v] for k, v in a["disps"].most_common()
                          if k not in NON_CONVERSATION_DISPS][:6],
        }

    agent_rows = sorted((agent_row(n, a) for n, a in agents.items()),
                        key=lambda x: -x["talk_secs"])

    channel_rows = []
    for ch, c in sorted(channels.items(), key=lambda kv: -kv[1]["n"]):
        src = next((k[len("_src_"):] for k in c if k.startswith("_src_")), "unknown")
        channel_rows.append({
            "channel": ch, "source": src, "n": c["n"], "conn": c["conn"],
            "connect_pct": round(100.0 * c["conn"] / c["n"], 1) if c["n"] else 0.0,
            "secs": round(c["secs"]),
        })

    daily_rows = [{
        "date": dt, "outbound": daily[dt]["outbound"], "connected": daily[dt]["connected"],
        "over3": daily[dt]["over3"], "over10": daily[dt]["over10"],
        "secs": round(daily[dt]["ob_secs"]), "inbound": daily[dt]["inbound"],
        "ringex": daily[dt]["ringex"],
    } for dt in sorted(dates)]

    totals = {
        "interactions": total_rows,
        "agents": len(agents),
        "outbound": sum(a["outbound"] for a in agent_rows),
        "connected": sum(a["connected"] for a in agent_rows),
        "convos": sum(a["convos"] for a in agent_rows),
        "over3": sum(a["over3"] for a in agent_rows),
        "over10": sum(a["over10"] for a in agent_rows),
        "talk_secs": sum(a["talk_secs"] for a in agent_rows),
        "inbound": sum(a["inbound"] for a in agent_rows),
        "ringex": sum(a["ringex"] for a in agent_rows),
        "camp_n": src_totals["campaign"], "camp_secs": round(src_secs["campaign"]),
        "pers_n": src_totals["personal"], "pers_secs": round(src_secs["personal"]),
        "date_start": daily_rows[0]["date"] if daily_rows else None,
        "date_end": daily_rows[-1]["date"] if daily_rows else None,
    }

    return {
        "status": "ok",
        "totals": totals,
        "agents": agent_rows,
        "channels": channel_rows,
        "daily": daily_rows,
        "dispositions": [[k, v] for k, v in all_disps.most_common()],
    }
