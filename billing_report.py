"""Per-agent call KPIs for the billing team, from the RingEX call log.

Pure logic: no Flask, no network. Takes the per-agent row lists that
RingCXClient.fetch_extension_calls() returns and produces a scoreboard with each
agent measured against a configurable KPI set.

Why this is a separate module from v5_report:
  * The sales board reconciles RingEX against RingCX because sales dials a RingCX
    campaign. Billing does NOT -- Danny confirmed they are RingEX only, and the
    RingCX CDR is 403 on this account anyway. So there is nothing to reconcile,
    and pretending otherwise would add a fake second source.
  * Billing is measured per SEAT, not per campaign. Every row already arrives
    attributed to the extension it was pulled for, so there is no name matching
    and no unattributed pile.

THE ONE TRAP THIS MODULE EXISTS TO AVOID
----------------------------------------
RingEX reports a nonzero `duration` on calls that were never answered -- it is
ring time, not talk time. Measured over 2026-05-26..08-23 across four billing
seats, summing it raw added 31 hours of conversation that never happened (+8%).
The tell is that Outbound/"Hang Up" tops out at exactly 80s and Outbound/"No
Answer" is 80s for every single row: that is the ring timeout, not a call.

So talk time is summed over CONNECTED calls only, and "calls over N minutes"
counts connected calls only -- a call that rang 130 seconds and was never picked
up is not a two-minute conversation.

Unrecognised result codes are counted into report["warnings"] rather than
falling through to "not connected". An absent answer must never be
representable as a negative answer.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

# A call is a real conversation past these marks (seconds). 120 is the one Danny
# asked for; the others give the board somewhere to grow without a schema change.
CONV_MARKS = (60, 120, 300)

# The default "long call" mark used for the headline KPI.
LONG_CALL_SECONDS = 120

# The only two states in which RingEX has actually connected two people.
_CONNECTED = {("Outbound", "Call connected"), ("Inbound", "Accepted")}

# Everything RingEX has been observed to emit on this account. A result outside
# this set is not silently treated as "not connected" -- it raises a warning, so
# a new RingCentral status code shows up as a question rather than as a quiet
# drop in everyone's talk time.
_KNOWN_RESULTS = {
    # outbound
    "Call connected", "Hang Up", "No Answer", "Wrong Number", "Not Allowed",
    "Call Failed", "Busy", "Stopped", "IP Phone Offline", "Rejected",
    # inbound
    "Accepted", "Missed", "Voicemail", "Reply", "Blocked", "Receive Error",
    "Call connected ", "Unknown",
}

# Defaults derived from the team's own 90-day distribution (2026-05-26..08-23,
# 257 agent-days): floor = 25th percentile of observed days, target = median,
# stretch = 75th. So the floor is a day three quarters of the team already beat
# and the target is a genuine coin flip -- which is where a new KPI has to sit if
# it is going to move anything without being written off as unreachable.
DEFAULT_TARGETS = {
    "talk_minutes":    {"floor": 60, "target": 85,  "stretch": 110},
    "calls":           {"floor": 40, "target": 55,  "stretch": 70},
    "connected":       {"floor": 35, "target": 50,  "stretch": 65},
    "long_calls":      {"floor": 6,  "target": 10,  "stretch": 12},
    # Computed and returned, but NOT shown on the board. Danny's read is that
    # unanswered inbound is queue traffic that rolls on to another agent, so
    # scoring an individual on it would penalise them for a call the system took
    # away. (Measured against these four seats it does NOT look like overflow --
    # 0.9% of missed calls were picked up by another of them within five minutes
    # and no session id was ever shared -- but that only rules out overflow
    # WITHIN this group, not to agents outside it.) Left in the payload so it can
    # be put back with one line if the routing is ever confirmed.
    "answer_rate_pct": {"floor": 35, "target": 50,  "stretch": 65},
}


# ── intraday pace ──────────────────────────────────────────────
# What fraction of a normal day's work is done by the END of each local hour.
# Measured over the 257 verified working agent-days (2026-05-26..08-23); talk,
# calls, connected and 2-minute calls all tracked within ~2 points of each other,
# so one curve serves all four.
#
# This exists because pro-rating a target by the CLOCK is badly wrong here. A
# 9-to-6 linear model says 56% of the day is gone by 2pm; the real figure is 37%,
# and at 5pm the model says 100% while the floor is at 80% and still working. Use
# the clock and the whole team reads "behind" every afternoon.
_PACE_CURVE_DEFAULT = [
    0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000,   # 00-07
    0.002, 0.005, 0.075, 0.169, 0.262, 0.366, 0.490, 0.589,   # 08-15
    0.682, 0.799, 0.890, 0.947, 0.976, 0.998, 1.000, 1.000,   # 16-23
]

# Below this many working days of a seat's own history, its curve is noise and
# the pooled default is the better estimate.
MIN_DAYS_FOR_OWN_CURVE = 10

# How much of the day must have elapsed before "ahead of / behind pace" means
# anything. Below this the denominator is a sliver of a target and the ratio is
# arithmetic noise: at 9am the curve says half a percent of the day is gone, so
# one answered call reads as several hundred percent of expected. That is what
# put agents on the board at 200%. Projection already stopped here; the RATIO
# did not, and the ratio is the number on the board.
MIN_FRAC_TO_JUDGE = 0.15


def build_pace_curve(day_hour_totals):
    """A seat's own intraday curve from its history.

    day_hour_totals -- [{hour: value}, ...], one dict per working day.

    Per-seat and not pooled because the shifts genuinely differ: over the
    verified window Gabriela Maldonado logged nothing before 1pm and was only 72%
    done by 6pm, while the other three were within five points of each other.
    Judged against a pooled curve she would read as far behind every morning for
    no reason other than starting later.

    Returns None when there is too little history to be worth trusting.
    """
    days = [d for d in (day_hour_totals or []) if sum(d.values()) > 0]
    if len(days) < MIN_DAYS_FOR_OWN_CURVE:
        return None
    curve = []
    for hr in range(24):
        acc = 0.0
        for d in days:
            total = sum(d.values())
            acc += sum(v for h, v in d.items() if h <= hr) / total
        curve.append(acc / len(days))
    # Force monotonic: a cumulative fraction that dips is a rounding artefact and
    # would make "expected by now" go backwards as the clock advances.
    for i in range(1, 24):
        curve[i] = max(curve[i], curve[i - 1])
    return curve


def pace_fraction(curve, hour, minute=0):
    """How much of a normal day is done at hour:minute, interpolated inside the
    hour so the expectation creeps rather than jumping on the hour."""
    curve = curve or _PACE_CURVE_DEFAULT
    hour = max(0, min(23, int(hour)))
    prev = curve[hour - 1] if hour > 0 else 0.0
    frac = prev + (curve[hour] - prev) * (max(0, min(59, minute)) / 60.0)
    return max(0.0, min(1.0, frac))


def _ts(value, tz_offset_minutes=0):
    """RingEX startTime -> local datetime. None when it cannot be read."""
    if not value:
        return None
    try:
        v = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc) + timedelta(minutes=tz_offset_minutes)
    except (ValueError, TypeError):
        return None


def is_connected(row) -> bool:
    return (row.get("direction"), row.get("result")) in _CONNECTED


def _blank_bucket():
    b = {
        # `calls` is every record on the seat. `handled_calls` is the one the KPI
        # uses: outbound dials plus inbound the agent actually picked up.
        # Unanswered inbound is excluded because Danny's read is that it is queue
        # traffic which rolls on to another agent -- counting it would credit, and
        # in the answer-rate case penalise, a person for a call the system moved.
        # It is 12.6% of all records across the verified 90 days, so the two
        # definitions are NOT interchangeable and the targets below are set
        # against `handled_calls`.
        "calls": 0, "handled_calls": 0, "dials": 0, "inbound": 0,
        "inbound_answered": 0, "connected": 0, "talk_seconds": 0,
        # After-call work, carried so the board can be RECONCILED with the
        # RingCX Interaction Report, whose "Handle Time" column is exactly
        # talk + wrap. Comparing the two without knowing that reads as a
        # discrepancy: Sarahi Rivera showed 95.5m here against 121.6m there,
        # which is 24 seconds of wrap on each of 64 calls.
        "wrap_seconds": 0,
        "longest_seconds": 0, "unreadable_time": 0,
    }
    for m in CONV_MARKS:
        b[f"over_{m}s"] = 0
    return b


def _fold(bucket, row):
    """Add one call to a bucket. The only place talk time is ever incremented."""
    bucket["calls"] += 1
    if row.get("direction") == "Outbound":
        bucket["dials"] += 1
        bucket["handled_calls"] += 1
    elif row.get("direction") == "Inbound":
        bucket["inbound"] += 1
        if row.get("result") == "Accepted":
            bucket["inbound_answered"] += 1
            bucket["handled_calls"] += 1

    if not is_connected(row):
        return                      # ring time is not talk time. See module docstring.

    try:
        dur = int(row.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    if dur < 0:
        dur = 0
    bucket["connected"] += 1
    bucket["talk_seconds"] += dur
    try:
        bucket["wrap_seconds"] += max(0, int(float(row.get("wrap_seconds") or 0)))
    except (TypeError, ValueError):
        pass
    bucket["longest_seconds"] = max(bucket["longest_seconds"], dur)
    for m in CONV_MARKS:
        if dur >= m:
            bucket[f"over_{m}s"] += 1


def _rate(num, den):
    return round(100.0 * num / den, 1) if den else 0.0


def grade(value, spec):
    """Where a value lands against a floor/target/stretch spec."""
    if value >= spec["stretch"]:
        return "stretch"
    if value >= spec["target"]:
        return "target"
    if value >= spec["floor"]:
        return "floor"
    return "below"


def build_report(rows_by_agent, *, default_curve=None, tz_offset_minutes=0, window=None,
                 targets=None, long_call_seconds=LONG_CALL_SECONDS,
                 roster_meta=None, now_local=None, curves=None):
    """Build the billing scoreboard.

    rows_by_agent -- {display_name: [call rows]}. Rows are RingEX call-log
                     records, already attributed by the extension they came from.
                     An agent with an empty list is NOT dropped: a seat that
                     logged nothing is a finding, not an absence.
    tz_offset_minutes -- signed offset EAST of UTC (Eastern DST = -240).
    """
    targets = dict(DEFAULT_TARGETS, **(targets or {}))
    warnings = []
    unknown_results = {}
    curves = curves or {}

    # Pace only means something for ONE day that is still running. Over a range,
    # or on a finished day, "expected by now" is just the target.
    win = window or {}
    live = bool(now_local and win.get("start") and win["start"] == win.get("end")
                and win["start"] == now_local.date().isoformat())

    agents = []
    for name, meta in sorted(rows_by_agent.items()):
        rows = meta["rows"] if isinstance(meta, dict) else meta
        ext = (meta.get("ext") if isinstance(meta, dict) else "") or ""
        ext_id = (meta.get("ext_id") if isinstance(meta, dict) else "") or ""
        # Did we actually READ this seat's window, or only part of it? A fetch
        # that failed and a phone that never rang produce the same empty list,
        # and on a board that names individuals the difference is the whole
        # point: one is a rate limit, the other is an accusation.
        complete = True if not isinstance(meta, dict) else bool(meta.get("complete", True))
        missing_days = (meta.get("missing_days") or []) if isinstance(meta, dict) else []

        total = _blank_bucket()
        by_day = {}
        for r in rows:
            res = r.get("result")
            if res not in _KNOWN_RESULTS:
                unknown_results[res] = unknown_results.get(res, 0) + 1
            _fold(total, r)
            t = _ts(r.get("start_time") or r.get("startTime"), tz_offset_minutes)
            if t is None:
                total["unreadable_time"] += 1
                continue
            _fold(by_day.setdefault(t.date().isoformat(), _blank_bucket()), r)

        # A working day is a day the seat CONNECTED at least one call. Weekend
        # days where a handful of dials fire and nothing connects are not
        # zero-productivity days, they are days off, and averaging them in
        # understates everyone. Reported separately as `idle_days` so the
        # exclusion is visible rather than silent.
        worked = {d: b for d, b in by_day.items() if b["connected"] > 0}
        idle = sorted(d for d, b in by_day.items() if b["connected"] == 0)
        n = len(worked) or 1

        # Every per-day figure divides WORKED-DAY work by WORKED days. Dividing
        # all-window work by worked days instead would credit the stray dials that
        # fire on an idle day to the days actually worked -- small, but it inflates
        # exactly the numbers the KPI is judged on. `total` still holds everything
        # in the window, so the two are available side by side rather than one
        # quietly standing in for the other.
        wtot = _blank_bucket()
        for b in worked.values():
            for k, v in b.items():
                wtot[k] += v
        wtot["longest_seconds"] = max([b["longest_seconds"] for b in worked.values()] or [0])

        per_day = {
            "calls": round(wtot["handled_calls"] / n, 1),
            "connected": round(wtot["connected"] / n, 1),
            "long_calls": round(wtot[f"over_{long_call_seconds}s"] / n, 1),
            "talk_minutes": round(wtot["talk_seconds"] / 60.0 / n, 1),
        }
        answer_rate = _rate(wtot["inbound_answered"], wtot["inbound"])

        scored = {
            "talk_minutes": per_day["talk_minutes"],
            "calls": per_day["calls"],
            "connected": per_day["connected"],
            "long_calls": per_day["long_calls"],
            "answer_rate_pct": answer_rate,
        }
        grades = {k: grade(v, targets[k]) for k, v in scored.items()}
        # Headline band = how the person is doing on the metric the KPI leads
        # with. Talk time is the one Danny named first.
        band = grades["talk_minutes"]

        pace = None
        if live:
            own = curves.get(name)
            # A seat with too little history of its own falls back to its TEAM's
            # curve, not to billing's. Scheduling and Inbound take queue calls
            # from the moment they log on; billing dials out and is barely
            # started before 10. Measured over 160 and 174 working agent-days,
            # inbound is 4.7% done by 9am where billing's curve says 0.5% -- so
            # judging them against billing's morning read as ~8x expected.
            frac = pace_fraction(own or default_curve, now_local.hour, now_local.minute)
            today_b = by_day.get(now_local.date().isoformat()) or _blank_bucket()
            actual = {
                "talk_minutes": round(today_b["talk_seconds"] / 60.0, 1),
                "calls": today_b["handled_calls"],
                "connected": today_b["connected"],
                "long_calls": today_b[f"over_{long_call_seconds}s"],
            }
            pace = {"fraction": round(frac, 3),
                    "curve": "own" if own else "team",
                    "as_of": now_local.strftime("%-I:%M %p"),
                    "actual": actual, "expected": {}, "projected": {}, "ratio": {},
                    "grades": {}}
            for k, v in actual.items():
                tgt_full = targets[k]["target"]
                exp = tgt_full * frac
                pace["expected"][k] = round(exp, 1)
                # Projecting from a sliver of the day is arithmetic, not insight:
                # at 9:05am one connected call extrapolates to a heroic day. Below
                # 15% elapsed there is no projection, and the board says so.
                judge = frac >= MIN_FRAC_TO_JUDGE
                proj = round(v / frac, 1) if judge else None
                pace["projected"][k] = proj
                pace["ratio"][k] = round(v / exp, 2) if (judge and exp > 0) else None
                # Colour the live day by where it is HEADED, not by the fraction
                # of a target a half-finished day has reached.
                pace["grades"][k] = grade(proj, targets[k]) if proj is not None else None
            pace["projectable"] = frac >= MIN_FRAC_TO_JUDGE

        daily_talk = sorted(b["talk_seconds"] / 60.0 for b in worked.values())
        agents.append({
            "pace": pace,
            "name": name, "ext": ext, "ext_id": ext_id,
            "totals": total, "worked": wtot,
            "worked_days": len(worked), "idle_days": len(idle), "idle_day_list": idle,
            "per_day": per_day, "scored": scored, "grades": grades, "band": band,
            "answer_rate_pct": answer_rate,
            "wrap_minutes": round(wtot["wrap_seconds"] / 60.0, 1),
            "handle_minutes": round((wtot["talk_seconds"] + wtot["wrap_seconds"]) / 60.0, 1),
            "avg_call_seconds": round(wtot["talk_seconds"] / wtot["connected"])
                                if wtot["connected"] else 0,
            "long_call_share_pct": _rate(wtot[f"over_{long_call_seconds}s"],
                                         wtot["connected"]),
            "median_talk_minutes": round(statistics.median(daily_talk), 1) if daily_talk else 0.0,
            "days": [dict(by_day[d], date=d,
                          talk_minutes=round(by_day[d]["talk_seconds"] / 60.0, 1),
                          long_calls=by_day[d][f"over_{long_call_seconds}s"],
                          worked=by_day[d]["connected"] > 0)
                     for d in sorted(by_day)],
            "complete": complete, "missing_days": missing_days,
            # An empty seat is only a FINDING when the window was actually read.
            # If the fetch was incomplete, an empty result means "we don't know",
            # and saying "logged no calls at all" would be inventing a fact.
            "no_activity": total["calls"] == 0 and complete,
            # Dialled, but nothing ever connected. Not the same as a quiet day and
            # not the same as an empty seat -- this is what a dead line looks like.
            "no_connections": total["calls"] > 0 and len(worked) == 0 and complete,
            "unknown": total["calls"] == 0 and not complete,
        })

    # Rank by talk time per working day -- the headline KPI.
    #
    # Two states are held OUT of the ranking rather than placed at the bottom of
    # it. Ranking a seat 'last' with zeros says they competed and lost; a seat
    # with no calls at all, or one that dialled all window and never connected
    # once, has not competed -- it has a problem. Ana Salazar's extension did
    # exactly this from 2026-08-10 (2-9 dials a day, zero connections, eleven
    # straight working days), and reading that as last place would have put a
    # dead handset on a performance board.
    ranked = sorted([a for a in agents if not a["no_activity"]
                     and not a["no_connections"] and not a["unknown"]],
                    key=lambda a: -a["per_day"]["talk_minutes"])
    silent = [a for a in agents if a["no_activity"]]
    stalled = [a for a in agents if a["no_connections"]]
    unknown = [a for a in agents if a["unknown"]]

    team = _blank_bucket()
    for a in agents:
        for k, v in a["worked"].items():
            team[k] += v
    team_days = sum(a["worked_days"] for a in agents) or 1
    team_summary = {
        "totals": team, "agent_days": sum(a["worked_days"] for a in agents),
        "per_day": {
            "calls": round(team["handled_calls"] / team_days, 1),
            "connected": round(team["connected"] / team_days, 1),
            "long_calls": round(team[f"over_{long_call_seconds}s"] / team_days, 1),
            "talk_minutes": round(team["talk_seconds"] / 60.0 / team_days, 1),
        },
        "answer_rate_pct": _rate(team["inbound_answered"], team["inbound"]),
        "unanswered_inbound": team["inbound"] - team["inbound_answered"],
    }

    if unknown_results:
        warnings.append({
            "kind": "unknown_result",
            "message": ("RingEX returned result codes this report has never seen. They were "
                        "counted as calls but NOT as talk time, which may be wrong. "
                        "Check them before trusting the talk figures."),
            "values": sorted(unknown_results.items(), key=lambda x: -x[1]),
        })
    bad_time = sum(a["totals"]["unreadable_time"] for a in agents)
    if bad_time:
        warnings.append({
            "kind": "unreadable_timestamp",
            "message": (f"{bad_time} call(s) had a start time that could not be read, so they "
                        f"count in the totals but sit in no day and no daily average."),
        })
    for a in silent:
        warnings.append({
            "kind": "no_activity",
            "message": (f"{a['name']} (ext {a['ext']}) logged no calls at all in this window. "
                        f"That is an empty seat, a wrong extension, or someone who does not "
                        f"dial from RingEX -- not a zero score."),
        })
    for a in stalled:
        t = a["totals"]
        warnings.append({
            "kind": "no_connections",
            "message": (f"{a['name']} (ext {a['ext']}) logged {t['calls']} call(s) in this "
                        f"window -- {t['dials']} outbound, {t['inbound']} inbound -- and "
                        f"connected none of them. A line that logs calls but never connects is "
                        f"out, reassigned, or broken -- it is not last place, so this seat is "
                        f"held out of the ranking."),
        })

    for a in unknown:
        warnings.append({
            "kind": "unknown_seat",
            "message": (f"{a['name']} (ext {a['ext']}) returned no calls, but this seat's window "
                        f"was NOT fully read -- so this is 'not known', not 'no calls'."),
        })
    # One line for the whole team, not one per person. Five near-identical
    # sentences saying "missing 1 day" is noise that buries the notes that
    # actually differ.
    partial = [a for a in ranked + stalled + silent if not a["complete"]]
    if partial:
        worst = max(len(a["missing_days"]) for a in partial)
        who = ", ".join(a["name"] for a in partial[:4])
        if len(partial) > 4:
            who += " and %d more" % (len(partial) - 4)
        warnings.append({
            "kind": "partial_seat",
            "message": (f"{who} {'is' if len(partial) == 1 else 'are'} missing up to "
                        f"{worst} day(s) of call data, so every figure shown for "
                        f"{'them' if len(partial) == 1 else 'those seats'} is a floor, "
                        f"not a total."),
        })

    return {
        "live": live,
        "as_of": now_local.strftime("%-I:%M %p") if now_local else None,
        "ranked": ranked,
        "silent": silent,
        "stalled": stalled,
        "unknown": unknown,
        "team": team_summary,
        "targets": targets,
        "long_call_seconds": long_call_seconds,
        "conv_marks": list(CONV_MARKS),
        "window": window or {},
        "warnings": warnings,
        "meta": {"roster": roster_meta or {}, "agents": len(agents),
                 "source": "ringex_extension_call_log"},
    }
