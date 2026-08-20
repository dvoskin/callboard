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
# Fewer logged calls than this and an agent is not ranked -- part shift or a logging
# problem, not a slow day. Ranking on one logged call starts the wrong conversation.
MIN_CALLS_TO_RANK = 10

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


def build_report(exrows, cxrows, tz_offset_minutes=0, window=None):
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

    def add(agent, src, talk, connected, missed):
        name = (agent or "").strip() or "Unassigned"
        led.setdefault(name, []).append(
            {"src": src, "talk": max(0.0, talk), "conn": connected, "missed": missed})

    def cx_talk(r):
        t = _num(r.get("talk_time"))
        return t if t > 0 else _num(r.get("duration"))

    for r in cxcamp:
        st = classify_result(r.get("result"))
        add(r.get("agent_name"), "campaign", cx_talk(r), st == "connected", st == "missed")
    for e, u, _ in pairs:                      # counted ONCE, on the RingCX timing
        st = classify_result(e.get("result"))
        add(u.get("agent_name") or e.get("agent_name"), "direct",
            cx_talk(u), st == "connected", st == "missed")
    for u in uc_unmatched:
        st = classify_result(u.get("result"))
        add(u.get("agent_name"), "direct_cx_only", cx_talk(u),
            st == "connected" or cx_talk(u) > 0, st == "missed")
    for e in exrows:
        if id(e) in matched_ex:
            continue
        st = classify_result(e.get("result"))
        add(e.get("agent_name"), "direct_ex_only",
            _num(e.get("duration")) if st == "connected" else 0.0,
            st == "connected", st == "missed")

    NOT_PEOPLE = {"", "Unassigned", "HR Department", "IVR Main Menu 1001", "GOALS PLASTIC S"}
    agents = []
    for name, L in led.items():
        if name in NOT_PEOPLE:
            continue
        talk = sum(x["talk"] for x in L)
        camp = sum(x["talk"] for x in L if x["src"] == "campaign")
        a = {"name": name, "talk": round(talk), "campaign": round(camp),
             "direct": round(talk - camp), "attempts": len(L),
             "missed": sum(1 for x in L if x["missed"]),
             "longest": round(max((x["talk"] for x in L), default=0))}
        for m in CONV_MARKS:
            a["over_%d" % (m // 60)] = sum(1 for x in L if x["talk"] >= m)
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
                 "min_calls_to_rank": MIN_CALLS_TO_RANK},
        "floor": med,
        "ranked": ranked, "unranked": thin,
        "totals": {k: sum(a[k] for a in agents) for k in
                   ("talk", "campaign", "direct", "attempts", "missed",
                    "over_1", "over_3", "over_10")},
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
