"""Per-agent talk-time and call-performance report, reconciled across RingEX and RingCX.

Pure logic: no Flask, no network. Takes the row lists that
RingCXClient._fetch_ringex_agent_calls() and ._fetch_ringcx_cdr_rows() return and
produces one merged call ledger with no double counting.

Why a reconciliation and not just one source:
  * RingCX only writes a record once a call CONNECTS. Rang-out and voicemail calls
    exist only in RingEX.
  * RingCX carries campaign/dialer traffic that never touches a RingEX user extension.
  * Measured on the 2026-08-20 exports, RingCX's UC lane held 20 of the 46 connected
    RingEX calls. Either source alone understates the floor.

Nothing is silently discarded. Rows whose result or timestamp can't be interpreted
are counted into report["warnings"] rather than defaulting to "not connected" --
an absent answer must never be representable as a negative answer.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

# A call is a "real conversation" past these marks (seconds).
CONV_MARKS = (60, 180, 600)
# Two calls are the same call if both phone numbers match and they start this close.
MATCH_TOLERANCE_SECONDS = 180
# One logged call is enough to be ranked. It was 10, on the reasoning that a part
# shift should not be scored -- but for most of the morning that put nearly the
# whole floor in an unranked pile, which is worse: a rep with 3 calls at 09:30 is
# not unrankable, they are early. Only someone with NO calls stays out, and they
# appear in the "no calls yet" group instead.
#
# The floor is a median, so it moves with the shift rather than being a target;
# early in the day it is genuinely low, and that is the honest reading.
MIN_CALLS_TO_RANK = 1

# How many channels the expand panel lists before the tail is rolled into one line.
CHANNELS_SHOWN = 5


def _norm(n):
    return " ".join((n or "").split()).lower()


def channel_label(raw, agent_name=""):
    """RingCX's Channel value as a person would say it.

    The raw names are operational -- "v2 ENG - New Lead - Scheduled",
    "New_Bilingual_IB", "UC" -- and reading five of those in a panel is work.
    Danny's mapping, applied BEFORE grouping so the variants merge into one line
    instead of sitting next to each other:

      the agent's own name  -> Personal queue   (RingCX names it after them)
      UC, RingEX direct     -> RingEX           (both are the rep's own line)
      ... New Lead ... Scheduled -> Scheduled Calls   (ENG and ESP alike)
      ... New Lead ... Call Now  -> Call Now
      ..._IB, DDR ... New Patient -> Inbound

    Anything else is left exactly as RingCX wrote it. Manual Upload, Retry and
    Follow Up are untouched on purpose: renaming a queue nobody asked about would
    be inventing a category, and a wrong label is worse than a long one.
    """
    raw = (raw or "").strip()
    low = _norm(raw)
    if not raw:
        return "Unknown"
    if agent_name and low == _norm(agent_name):
        return "Personal queue"
    if low in ("uc", "ringex direct"):
        return "RingEX"
    if "new lead" in low:
        if "scheduled" in low:
            return "Scheduled Calls"
        if "call now" in low:
            return "Call Now"
    if low.endswith("_ib") or low.endswith(" ib"):
        return "Inbound"
    if low.startswith("ddr") and "new patient" in low:
        return "Inbound"
    return raw

# "uc call" is RingCX's result for an agent's own-line call. RingCX only writes a UC
# record once the call connects, so the value itself means connected.
_CONNECTED = ("connect", "accept", "answered", "completed", "call connected", "uc call")
_MISSED = ("missed", "voicemail", "vm/", "no answer", "abandon", "rejected",
           "declined", "busy", "unavailable", "congestion", "intercept")


def classify_result(result: str) -> str:
    """-> 'connected' | 'missed' | 'unknown'. Never guesses: an unrecognised value
    returns 'unknown' and is surfaced as a warning, never folded into 'missed'."""
    r = (result or "").strip().lower()
    if not r:
        return "unknown"
    # order matters: "no answer" must beat "answered", "outbound no answer" too
    for m in _MISSED:
        if m in r:
            return "missed"
    for c in _CONNECTED:
        if c in r:
            return "connected"
    return "unknown"


_IN_WORDS = ("inbound", "incoming")
_OUT_WORDS = ("outbound", "outgoing", "manual", "preview", "predictive", "progressive", "dialer")


def classify_direction(raw: str) -> str:
    """-> 'in' | 'out' | 'unknown'. Like classify_result it never guesses: an
    unrecognised value is 'unknown' and is surfaced as a warning.

    RingEX writes Inbound / Outbound; the Interaction Report's Call Type and
    the live CDR's Call Direction carry the same words, and a dialer campaign
    (manual, preview, predictive) is outbound by definition. Danny,
    2026-10-15: "show inbound vs outbound calls on the sales call tracker".
    """
    r = (raw or "").strip().lower()
    if not r:
        return "unknown"
    if any(w in r for w in _IN_WORDS) or r in ("in", "ib"):
        return "in"
    if any(w in r for w in _OUT_WORDS) or r in ("out", "ob"):
        return "out"
    return "unknown"


def _row_direction(row, channel=""):
    """A call's direction from its own field; the channel only when that is
    silent -- an inbound queue (..._IB, DDR New Patient) is inbound, a New Lead
    campaign (Scheduled Calls, Call Now) is outbound."""
    d = classify_direction(row.get("direction") or row.get("call_direction"))
    if d != "unknown":
        return d
    lab = channel_label(channel or row.get("channel") or row.get("campaign_name"), row.get("agent_name") or "")
    if lab == "Inbound":
        return "in"
    if lab in ("Scheduled Calls", "Call Now"):
        return "out"
    return "unknown"


def _party(row, dirn):
    """The other party's ten digits: the patient or lead on the line.

    The Interaction Report's Lead Phone (ani) is the lead in BOTH directions;
    RingEX names the caller on an inbound and the callee on an outbound; the
    live CDR's ANI is the caller, DNIS the callee."""
    if row.get("source") == "ringcx_csv":
        v = row.get("ani")
    elif "from_number" in row or "to_number" in row:
        v = row.get("from_number") if dirn == "in" else row.get("to_number")
    else:
        v = row.get("ani") if dirn == "in" else row.get("dnis")
    d = "".join(ch for ch in str(v or "") if ch.isdigit())[-10:]
    return d if len(d) == 10 else ""


def _fmt_phone(d):
    return "(%s) %s-%s" % (d[:3], d[3:6], d[6:]) if len(d) == 10 else d


def norm_phone(v) -> str:
    """Last 10 digits, so +1 929 419-5259 / 9294195259 / (929) 419-5259 all match."""
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p",
)


def parse_ts(v, tz_offset_minutes: int = 0):
    """Best-effort timestamp -> aware UTC datetime, or None (counted as a warning).

    Epoch millis/seconds, ISO-8601 with or without Z/offset, and the US formats the
    RingCX CDR uses. Naive values are read in the report's local zone, then converted."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        n = float(v)
        if n > 1e11:      # milliseconds
            n /= 1000.0
        if n < 1e6:       # not a plausible epoch
            return None
        return datetime.fromtimestamp(n, tz=timezone.utc)
    s = str(v).strip()
    if s.endswith("Z"):
        s2 = s[:-1] + "+0000"
    else:
        s2 = s
    # normalise "+00:00" -> "+0000" for %z on older Pythons
    if len(s2) > 6 and s2[-3] == ":" and (s2[-6] in "+-"):
        s2 = s2[:-3] + s2[-2:]
    for f in _TS_FORMATS:
        for cand in (s2, s):
            try:
                dt = datetime.strptime(cand, f)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(minutes=tz_offset_minutes)))
            return dt.astimezone(timezone.utc)
    return None


def _num(v) -> float:
    try:
        return float(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_uc(row) -> bool:
    """A RingCX row that is NOT dialer/queue traffic -- i.e. the agent's own line,
    which is the lane that mirrors RingEX. Campaign and gate rows never do."""
    camp = (row.get("campaign_name") or "").strip()
    if camp:
        return False
    q = (row.get("queue_name") or "").strip()
    ct = (row.get("call_type") or "").strip().lower()
    if "uc" in ct:
        return True
    # a queue/gate name means dialer or inbound-queue traffic
    return not q or q.upper() == "UC"


def match_calls(exrows, cxuc, tz_offset_minutes=0):
    """Pair RingEX calls with RingCX UC rows: BOTH phone numbers plus a close start.

    Single-ended matching is unsafe here -- +1 929 419 5259 is a shared main line
    used as the outbound caller ID by at least six reps, so matching on one end
    produces confident false pairs.
    """
    used, pairs, unmatched = set(), [], []
    prepared = []
    for i, u in enumerate(cxuc):
        prepared.append((i, norm_phone(u.get("ani")), norm_phone(u.get("dnis")),
                         parse_ts(u.get("start_time"), tz_offset_minutes)))
    for e in exrows:
        efrom, eto = norm_phone(e.get("from_number")), norm_phone(e.get("to_number"))
        et = parse_ts(e.get("start_time"), tz_offset_minutes)
        best = None
        if efrom and eto and et:
            for i, ani, dnis, ut in prepared:
                if i in used or not ut:
                    continue
                if ani != efrom or dnis != eto:
                    continue
                d = abs((ut - et).total_seconds())
                if d <= MATCH_TOLERANCE_SECONDS and (best is None or d < best[0]):
                    best = (d, i)
        if best:
            used.add(best[1])
            pairs.append((e, cxuc[best[1]], best[0]))
        else:
            unmatched.append(e)
    return pairs, unmatched, [u for i, u in enumerate(cxuc) if i not in used]


def build_report(exrows, cxrows, tz_offset_minutes=0, window=None, roster=None):
    """Merge both sources into one ledger and score every agent.

    exrows -- RingCXClient._fetch_ringex_agent_calls() output
    cxrows -- RingCXClient._fetch_ringcx_cdr_rows() output
    """
    exrows = list(exrows or [])
    cxrows = list(cxrows or [])
    warn = []

    cxuc = [r for r in cxrows if _is_uc(r)]
    cxcamp = [r for r in cxrows if not _is_uc(r)]
    pairs, ex_unmatched, uc_unmatched = match_calls(exrows, cxuc, tz_offset_minutes)
    matched_ex = {id(e) for e, _, _ in pairs}

    # ---- data-quality surfacing (never silently absorbed)
    unk = {}
    for r in exrows + cxrows:
        if classify_result(r.get("result")) == "unknown":
            k = (r.get("result") or "(blank)").strip() or "(blank)"
            unk[k] = unk.get(k, 0) + 1
    if unk:
        warn.append({"kind": "unclassified_result",
                     "detail": "Call results not recognised as connected or missed; "
                               "counted as attempts but never as conversations.",
                     "values": sorted(unk.items(), key=lambda kv: -kv[1])[:8]})
    bad_ts = sum(1 for r in exrows + cxrows if parse_ts(r.get("start_time"), tz_offset_minutes) is None)
    if bad_ts:
        warn.append({"kind": "unparsed_timestamp", "count": bad_ts,
                     "detail": "Rows whose start time could not be read. They still count "
                               "toward talk time but cannot be matched across systems."})
    if exrows and not cxrows:
        warn.append({"kind": "source_empty", "detail": "RingCX returned no interactions."})
    if cxrows and not exrows:
        warn.append({"kind": "source_empty",
                     "detail": "RingEX returned no calls. If this is a working day it is a "
                               "fetch failure, not a quiet phone -- an empty list and a quiet "
                               "day look identical."})

    # ---- one ledger, each call once
    led: dict[str, list] = {}

    dir_unk = {}

    def add(agent, src, talk, connected, missed, wrap=0.0, channel="", dirn="unknown", raw_dir="", row=None):
        name = (agent or "").strip() or "Unassigned"
        if dirn == "unknown":
            k = (raw_dir or "(blank)").strip() or "(blank)"
            dir_unk[k] = dir_unk.get(k, 0) + 1
        led.setdefault(name, []).append(
            {"src": src, "talk": max(0.0, talk), "conn": connected, "missed": missed,
             "wrap": max(0.0, wrap), "chan": (channel or "").strip(), "dir": dirn,
             # who was on the other end, and when -- for the missed-call list
             "num": _party(row or {}, dirn), "ts": parse_ts((row or {}).get("start_time"), tz_offset_minutes)})

    def cx_talk(r):
        t = _num(r.get("talk_time"))
        return t if t > 0 else _num(r.get("duration"))

    for r in cxcamp:
        st = classify_result(r.get("result"))
        # wrap comes from the emailed Interaction Report; the live CDR pull has no
        # wrap column, so it is 0 there rather than wrong.
        add(r.get("agent_name"), "campaign", cx_talk(r), st == "connected", st == "missed",
            _num(r.get("wrap_time")), channel=r.get("channel") or r.get("campaign_name"),
            dirn=_row_direction(r), raw_dir=r.get("direction"), row=r)
    for e, u, _ in pairs:                      # counted ONCE, on the RingCX timing
        st = classify_result(e.get("result"))
        # RingEX knows the direction of the rep's own line for certain.
        d_ = classify_direction(e.get("direction"))
        add(u.get("agent_name") or e.get("agent_name"), "direct",
            cx_talk(u), st == "connected", st == "missed",
            channel=u.get("channel") or "UC",
            dirn=d_ if d_ != "unknown" else _row_direction(u), raw_dir=e.get("direction") or u.get("direction"), row=e)
    for u in uc_unmatched:
        st = classify_result(u.get("result"))
        add(u.get("agent_name"), "direct_cx_only", cx_talk(u),
            st == "connected" or cx_talk(u) > 0, st == "missed",
            channel=u.get("channel") or "UC", dirn=_row_direction(u), raw_dir=u.get("direction"), row=u)
    for e in exrows:
        if id(e) in matched_ex:
            continue
        st = classify_result(e.get("result"))
        add(e.get("agent_name"), "direct_ex_only",
            _num(e.get("duration")) if st == "connected" else 0.0,
            st == "connected", st == "missed", channel="RingEX direct",
            dirn=classify_direction(e.get("direction")), raw_dir=e.get("direction"), row=e)

    if dir_unk:
        warn.append({"kind": "unclassified_direction",
                     "detail": "Calls whose direction could not be read; counted in Dials, "
                               "never as inbound or outbound.",
                     "values": sorted(dir_unk.items(), key=lambda kv: -kv[1])[:8]})

    # Missed inbound calls and whether anyone got back to the number: a later
    # outbound to it from ANY rep, or the number calling again and being
    # answered. Danny, 2026-10-15: "add missed calls to sales call tracker".
    by_num = {}
    for nm_, L_ in led.items():
        for x in L_:
            if x.get("num") and x.get("ts"):
                by_num.setdefault(x["num"], []).append((x["ts"], nm_, x.get("dir"), x["conn"]))
    for v in by_num.values():
        v.sort(key=lambda e: e[0])
    loc = timedelta(minutes=tz_offset_minutes)

    def _missed_for(name, L):
        out = []
        for x in L:
            if x.get("dir") != "in" or not x["missed"] or x["conn"]:
                continue
            back = None
            if x.get("num") and x.get("ts"):
                for t2, who, d2, c2 in by_num.get(x["num"], []):
                    if t2 > x["ts"] and (d2 == "out" or (d2 == "in" and c2)):
                        back = {"at": (t2 + loc).strftime("%-I:%M %p"), "by": who,
                                "how": "called back" if d2 == "out" else "called again, answered"}
                        break
            out.append({"ts": x.get("ts"), "at": (x["ts"] + loc).strftime("%-I:%M %p") if x.get("ts") else "",
                        "number": _fmt_phone(x.get("num") or ""), "resolved": back is not None, "back": back})
        out.sort(key=lambda m: m["ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for m in out:
            m.pop("ts", None)
        return {"missed_in": len(out), "missed_open": sum(1 for m in out if not m["resolved"]),
                "missed_calls": out[:25]}

    # "N/A" is what RingCX writes when a leg has no agent -- a queue leg nobody
    # picked up. It is not a person, and it is not a small row: 249 of them in a
    # single day's export. Ranked, it would sit on the sales board as an agent
    # with more calls than anyone real.
    NOT_PEOPLE = {"", "Unassigned", "HR Department", "IVR Main Menu 1001",
                  "GOALS PLASTIC S", "N/A", "n/a", "N/a"}
    agents = []
    not_sales = []
    for name, L in led.items():
        if name in NOT_PEOPLE:
            continue
        # The roster is applied HERE, before floor, bands and totals are derived,
        # so a non-sales rep never moves the median everyone else is measured
        # against. Vera Payne had 12 attempts, which is over MIN_CALLS_TO_RANK --
        # filtering her out downstream would have left her inside the floor.
        if roster is not None and " ".join((name or "").split()).lower() not in roster:
            not_sales.append(name)
            continue
        talk = sum(x["talk"] for x in L)
        camp = sum(x["talk"] for x in L if x["src"] == "campaign")
        a = {"name": name, "talk": round(talk), "campaign": round(camp),
             "direct": round(talk - camp), "attempts": len(L),
             "missed": sum(1 for x in L if x["missed"]),
             "connected": sum(1 for x in L if x["conn"]),
             "wrap": round(sum(x.get("wrap", 0.0) for x in L)),
             "longest": round(max((x["talk"] for x in L), default=0)),
             # Danny, 2026-10-15: inbound vs outbound on the sales board.
             "inbound": sum(1 for x in L if x.get("dir") == "in"),
             "inbound_answered": sum(1 for x in L if x.get("dir") == "in" and x["conn"]),
             "outbound": sum(1 for x in L if x.get("dir") == "out"),
             "outbound_connected": sum(1 for x in L if x.get("dir") == "out" and x["conn"]),
             "dir_unknown": sum(1 for x in L if x.get("dir") not in ("in", "out"))}
        a.update(_missed_for(name, L))
        for m in CONV_MARKS:
            a["over_%d" % (m // 60)] = sum(1 for x in L if x["talk"] >= m)

        # Talk time per channel. RingCX names an agent's own queue after the agent,
        # so "Charlotte McKay" as a Channel is her personal queue, not a campaign --
        # it is relabelled rather than shown as if it were one.
        by_chan = {}
        for x in L:
            key = channel_label(x.get("chan"), name)
            slot = by_chan.setdefault(key, {"name": key, "talk": 0.0, "calls": 0})
            slot["talk"] += x["talk"]
            slot["calls"] += 1
        chans = sorted(by_chan.values(), key=lambda c: -c["talk"])
        chans = [c for c in chans if c["talk"] > 0 or c["calls"] > 0]
        # Keep the panel short: the long tail becomes one line rather than twenty,
        # and it is labelled with its own count so nothing looks dropped.
        if len(chans) > CHANNELS_SHOWN:
            rest = chans[CHANNELS_SHOWN:]
            chans = chans[:CHANNELS_SHOWN] + [{
                "name": "%d more" % len(rest),
                "talk": sum(c["talk"] for c in rest),
                "calls": sum(c["calls"] for c in rest)}]
        a["channels"] = [{"name": c["name"], "talk": round(c["talk"]),
                          "calls": c["calls"]} for c in chans]
        agents.append(a)
    agents.sort(key=lambda a: -a["talk"])

    ranked = [a for a in agents if a["attempts"] >= MIN_CALLS_TO_RANK]
    thin = [a for a in agents if a["attempts"] < MIN_CALLS_TO_RANK]
    med = {}
    if ranked:
        med = {"talk": statistics.median([a["talk"] for a in ranked]),
               "attempts": statistics.median([a["attempts"] for a in ranked]),
               "over_3": statistics.median([a["over_3"] for a in ranked]),
               "over_10": statistics.median([a["over_10"] for a in ranked])}
        for a in ranked:
            b = [k for k, f in (("talk", "talk"), ("dials", "attempts"), ("quality", "over_3"))
                 if a[f] < med[{"talk": "talk", "dials": "attempts", "quality": "over_3"}[k]]]
            a["below"] = b
            a["band"] = "low" if len(b) == 3 else ("mid" if b else "ok")
    for a in thin:
        a["below"], a["band"] = [], "na"

    conn_unmatched = [e for e in ex_unmatched if classify_result(e.get("result")) == "connected"]
    by = {}
    for e in conn_unmatched:
        n = (e.get("agent_name") or "Unassigned").strip()
        by[n] = by.get(n, 0) + 1
    ucby = {}
    for u in uc_unmatched:
        n = (u.get("agent_name") or "Unassigned").strip()
        ucby[n] = ucby.get(n, 0) + 1

    ex_connected = sum(1 for e in exrows if classify_result(e.get("result")) == "connected")
    return {
        "meta": {"window": window or {}, "tz_offset_minutes": tz_offset_minutes,
                 "ringex_calls": len(exrows), "ringcx_rows": len(cxrows),
                 "ringcx_campaign": len(cxcamp), "ringcx_uc": len(cxuc),
                 "min_calls_to_rank": MIN_CALLS_TO_RANK,
                 "not_sales": sorted(set(not_sales))},
        "floor": med,
        "ranked": ranked, "unranked": thin,
        "totals": {k: sum(a[k] for a in agents) for k in
                   ("talk", "campaign", "direct", "attempts", "missed",
                    "over_1", "over_3", "over_10", "inbound", "inbound_answered",
                    "outbound", "outbound_connected", "dir_unknown", "missed_in", "missed_open")},
        "recon": {
            "matched": len(pairs),
            "ex_connected": ex_connected,
            "ex_only": len(conn_unmatched),
            "ex_only_seconds": round(sum(_num(e.get("duration")) for e in conn_unmatched)),
            "ex_only_by": sorted(by.items(), key=lambda kv: -kv[1]),
            "ex_missed_absent": sum(1 for e in ex_unmatched
                                    if classify_result(e.get("result")) == "missed"),
            "uc_only": len(uc_unmatched),
            "uc_only_by": sorted(ucby.items(), key=lambda kv: -kv[1]),
            "coverage_pct": round(len(pairs) / ex_connected * 100, 1) if ex_connected else None,
        },
        "warnings": warn,
    }


# ══════════════════════════════════════════════════════════════
# RingCX Interaction Report CSV  ->  the row shape the live CDR pull returns
# ══════════════════════════════════════════════════════════════
# The emailed/exported "Daily Interaction Report" is a DIFFERENT RingCX report
# from the GLOBAL_CALL_TYPE_DELIMITED CDR the API pulls, and it is the one whose
# figures were reconciled by hand. When a fresh one is available it is the
# authoritative RingCX source; the CDR pull is the fallback.
CX_REQUIRED_COLUMNS = ("Agent Full Name", "Interaction Start Time", "Channel Type",
                       "Talk Time (min)", "Sum of Interaction Duration")


class CsvShapeError(ValueError):
    """The uploaded file is not a RingCX Interaction Report."""


class EmptyReportError(CsvShapeError):
    """A correctly-shaped Interaction Report that contains no interactions.

    Subclasses CsvShapeError so every existing `except CsvShapeError` keeps its
    protective behaviour -- a STORED file that reads empty is still refused.
    Only callers that opt in (the ingest endpoint) treat it as the ordinary
    before-the-first-dial case that it usually is.
    """


def parse_interaction_csv(text: str):
    """Parse an Interaction Report export into CDR-shaped rows.

    Raises rather than returning a short list -- a silently empty parse is
    indistinguishable from a quiet day, and that is precisely the failure that hid
    the RingEX fetch bug for 41 days. The two causes are separate types:
    CsvShapeError for a file that is not an Interaction Report, EmptyReportError
    for one that is but holds no interactions yet.
    """
    import csv as _csv
    import io

    rdr = _csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = set(rdr.fieldnames or [])
    missing = [c for c in CX_REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise CsvShapeError(
            "Not a RingCX Interaction Report — missing column(s): %s. Found: %s"
            % (", ".join(missing), ", ".join(sorted(cols)[:8]) or "nothing"))

    rows, unit_sec, unit_min = [], 0, 0
    for r in rdr:
        if (r.get("Date") or "").strip().lower() == "average":
            continue
        if not (r.get("Agent Full Name") or "").strip():
            continue
        talk = _num(r.get("Talk Time (min)"))
        dur = _num(r.get("Sum of Interaction Duration"))
        if talk > 0 and dur > 0:
            if abs(talk - dur) < 1.0:
                unit_sec += 1
            elif abs(talk * 60 - dur) < 2.0:
                unit_min += 1
        uc = (r.get("Channel Type") or "").strip() == "UC Call"
        ch = (r.get("Channel") or "").strip()
        rows.append({
            "agent_name": (r.get("Agent Full Name") or "").strip(),
            "_talk_raw": talk, "duration": dur,
            "direction": (r.get("Call Type") or "").strip(),
            "result": (r.get("Call Result") or "").strip(),
            "start_time": "%s %s" % ((r.get("Date") or "").strip(),
                                     (r.get("Interaction Start Time") or "").split(" ")[0]),
            "ani": (r.get("Lead Phone") or "").strip().strip("'"),     # originating party
            "dnis": (r.get("Caller ID") or "").strip().strip("'"),     # destination
            "campaign_name": "" if uc else ch,
            # The raw Channel, kept for every row. campaign_name is blanked on UC
            # rows by design, so reusing it for the breakdown would silently drop
            # the agent's own line out of their own totals.
            "channel": ch,
            "queue_name": "UC" if uc else "",
            "call_type": "UC Call" if uc else "Voice",
            "agent_disposition": (r.get("Agent Disposition") or "").strip(),
            "_wrap_raw": _num(r.get("Wrap Time (min)")),
            "source": "ringcx_csv",
        })
    if not rows:
        # The header check above already rejected anything that is not an
        # Interaction Report, so reaching here means a real report covering a
        # stretch with no interactions. That is a different fact from "wrong
        # file", and conflating the two is what made every pre-dawn report an
        # error. Callers decide; the type carries the distinction.
        raise EmptyReportError(
            "Interaction Report is correctly shaped but contains no interactions.")

    # The columns say "(min)" but the CSV export writes SECONDS while the XLSX
    # export writes minutes. Decide per file by comparing against Sum of
    # Interaction Duration, which is always seconds.
    scale = 60.0 if unit_min > unit_sec else 1.0
    for r in rows:
        r["talk_time"] = r.pop("_talk_raw") * scale
        r["wrap_time"] = r.pop("_wrap_raw") * scale
    return rows, ("minutes" if scale == 60.0 else "seconds")
