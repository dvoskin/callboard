"""Unreturned inbound calls from booked patients.

Danny, 2026-10-13: "build the unreturned inbound calls board". The audit that
asked for it: nine paid patients were about to be dropped as "unresponsive";
the carrier log showed they had called us 30-odd times and we had missed most
of it and returned almost none of it. Jules called seven times before she
paid, every one missed. Downer called on Sep 4 and heard nothing for three
weeks.

This module is the pure part: give it the account-wide RingEX call log for a
window and the active Journeys with their numbers, and it returns, per
coordinator, the patients who called us and did not get a real call back.

Rules, chosen so the board cannot be satisfied by a voicemail drop:

  * A "miss" is an inbound call from a booked patient's number that nobody
    answered -- Missed, Voicemail, Hang Up, and the rest of RingEX's
    unanswered results. One inbound rings several phones and lands as several
    records sharing a session id; the session is answered if ANY leg was.
  * A miss is "returned" only by a real conversation afterwards: an outbound
    call to that number that connected for at least `min_connected` seconds
    (default 45 -- a 10-30 s "Call connected" is the voicemail greeting), or
    the patient calling again and being answered.
  * Anything shorter is a "try": counted and shown, never a resolution.
  * Repeated misses before the callback belong to one open thread.

Reads nothing from the network. The app supplies the rows.
"""
from datetime import datetime, timezone, timedelta

UNANSWERED = {"Missed", "Voicemail", "Hang Up", "Abandoned", "Busy", "Rejected",
              "No Answer", "Call Failed", "Declined", "Blocked", "Stopped"}
ANSWERED_IN = {"Accepted", "Answered", "Call connected"}
DEFAULT_MIN_CONNECTED = 45


def digits10(value) -> str:
    d = "".join(ch for ch in str(value or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def fmt_phone(d) -> str:
    d = digits10(d)
    return "(%s) %s-%s" % (d[:3], d[3:6], d[6:]) if len(d) == 10 else d


def _ts(value):
    """RingEX startTime -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        v = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _local(dt, tz_offset_minutes):
    """UTC -> wall clock, tz_offset_minutes being the browser's
    getTimezoneOffset() (minutes WEST of UTC, so EDT = 240)."""
    return dt - timedelta(minutes=tz_offset_minutes or 0)


def _when(dt, tz_offset_minutes):
    l = _local(dt, tz_offset_minutes)
    return {"iso": dt.isoformat(), "date": l.date().isoformat(),
            "label": l.strftime("%a %-m/%-d %-I:%M %p")}


def index_journeys(journeys):
    """digits -> [journey, ...]. Returns (index, journeys_without_a_number)."""
    by, missing = {}, 0
    for j in journeys or []:
        d = digits10(j.get("digits") or j.get("mobile") or "")
        if len(d) != 10:
            missing += 1
            continue
        by.setdefault(d, []).append(j)
    return by, missing


def _pick_journey(js, today):
    """One patient can hold two active Journeys (a rebooking kept the old
    case open). Show the one whose surgery is next; never hide the other."""
    def key(j):
        sd = j.get("surgery_date") or ""
        return (0 if sd >= today else 1, sd or "9999")
    js = sorted(js, key=key)
    return js[0], js[1:]


def build(calls, journeys, now=None, min_connected=DEFAULT_MIN_CONNECTED,
          tz_offset_minutes=0):
    now = now or datetime.now(timezone.utc)
    today = _local(now, tz_offset_minutes).date().isoformat()
    by_phone, no_number = index_journeys(journeys)

    # Every call to or from a booked number, grouped by number.
    events = {}
    scanned = 0
    for r in calls or []:
        scanned += 1
        direction = r.get("direction") or ""
        if direction not in ("Inbound", "Outbound"):
            continue
        # Faxes and texts share the call log ("Fax Receipt Error", "Received",
        # "Reply"); only a voice call is a call. Rows without a type are kept.
        if (r.get("type") or "Voice") != "Voice":
            continue
        t = _ts(r.get("start_time") or r.get("startTime"))
        if t is None:
            continue
        num = digits10(r.get("to_number") if direction == "Outbound" else r.get("from_number"))
        if len(num) != 10 or num not in by_phone:
            continue
        events.setdefault(num, []).append({
            "t": t, "dir": direction, "result": r.get("result") or "",
            "duration": int(r.get("duration") or 0), "agent": (r.get("agent_name") or "").strip(),
            "sid": r.get("session_id") or r.get("sessionId") or "",
        })

    rows = []
    for num, evs in events.items():
        # Inbound legs -> sessions. A call that rang three phones is one call.
        sessions = {}
        for e in evs:
            if e["dir"] != "Inbound":
                continue
            sid = e["sid"] or ("%s|%s" % (num, e["t"].isoformat()))
            s = sessions.setdefault(sid, {"t": e["t"], "answered": False, "agents": set(),
                                          "result": e["result"]})
            if e["t"] < s["t"]:
                s["t"] = e["t"]
            if e["result"] in ANSWERED_IN:
                s["answered"] = True
                s["result"] = e["result"]
            if e["agent"]:
                s["agents"].add(e["agent"])
        timeline = [("in", s) for s in sessions.values()] + \
                   [("out", e) for e in evs if e["dir"] == "Outbound"]
        timeline.sort(key=lambda x: x[1]["t"])

        open_since, misses, tries, last_try, rang = None, 0, 0, None, set()
        last_miss = None
        resolved = []          # threads closed inside the window
        for kind, x in timeline:
            if kind == "in":
                if x["answered"]:
                    if open_since is not None:
                        resolved.append({"opened": open_since, "closed": x["t"], "misses": misses,
                                         "tries": tries, "how": "called again, answered",
                                         "by": ", ".join(sorted(x["agents"])) or ""})
                        open_since, misses, tries, last_try, rang = None, 0, 0, None, set()
                else:
                    if open_since is None:
                        open_since, misses, tries, last_try, rang = x["t"], 1, 0, None, set()
                    else:
                        misses += 1
                    last_miss = x
                    rang |= x["agents"]
            else:
                if open_since is None:
                    continue
                connected = x["result"] == "Call connected" and x["duration"] >= min_connected
                if connected:
                    resolved.append({"opened": open_since, "closed": x["t"], "misses": misses,
                                     "tries": tries, "how": "called back", "by": x["agent"],
                                     "seconds": x["duration"]})
                    open_since, misses, tries, last_try, rang = None, 0, 0, None, set()
                else:
                    tries += 1
                    last_try = x

        if open_since is None and not resolved:
            continue

        j, others = _pick_journey(by_phone[num], today)
        sd = j.get("surgery_date") or ""
        try:
            days_until = (datetime.strptime(sd, "%Y-%m-%d").date()
                          - datetime.strptime(today, "%Y-%m-%d").date()).days
        except (ValueError, TypeError):
            days_until = None
        base = {
            "patient": j.get("name") or "", "journey_id": j.get("id") or "",
            "other_journeys": [{"id": o.get("id"), "owner": o.get("owner"),
                                "surgery_date": o.get("surgery_date")} for o in others],
            "number": fmt_phone(num), "digits": num,
            "owner": j.get("owner") or "(no owner)", "owner_id": j.get("owner_id") or "",
            "surgery_date": sd, "days_until": days_until,
            "stage": j.get("stage") or "", "date_status": j.get("date_status") or "",
            "language": j.get("language") or "",
        }
        if open_since is not None:
            age_h = (now - open_since).total_seconds() / 3600.0
            rows.append({**base, "status": "open",
                         "first_missed": _when(open_since, tz_offset_minutes),
                         "last_missed": _when(last_miss["t"], tz_offset_minutes) if last_miss else None,
                         "last_missed_result": (last_miss or {}).get("result", ""),
                         "misses": misses, "tries": tries,
                         "last_try": ({"at": _when(last_try["t"], tz_offset_minutes),
                                       "by": last_try["agent"], "result": last_try["result"],
                                       "seconds": last_try["duration"]} if last_try else None),
                         "rang": sorted(rang), "age_hours": round(age_h, 1)})
        for th in resolved:
            rows.append({**base, "status": "returned",
                         "first_missed": _when(th["opened"], tz_offset_minutes),
                         "misses": th["misses"], "tries": th["tries"],
                         "returned": {"at": _when(th["closed"], tz_offset_minutes),
                                      "how": th["how"], "by": th["by"],
                                      "seconds": th.get("seconds"),
                                      "hours_to_return": round((th["closed"] - th["opened"]).total_seconds() / 3600.0, 1)},
                         "age_hours": None})

    # Open first, oldest first; returned after, most recent first.
    rows.sort(key=lambda r: (0 if r["status"] == "open" else 1,
                             r["first_missed"]["iso"] if r["status"] == "open" else "",
                             "" if r["status"] == "open" else "~" + (r.get("returned") or {}).get("at", {}).get("iso", "")))
    returned_rows = [r for r in rows if r["status"] == "returned"]
    returned_rows.sort(key=lambda r: r["returned"]["at"]["iso"], reverse=True)
    rows = [r for r in rows if r["status"] == "open"] + returned_rows

    owners = {}
    for r in rows:
        o = owners.setdefault(r["owner"], {"owner": r["owner"], "owner_id": r["owner_id"],
                                           "open": 0, "returned": 0, "rows": []})
        o[r["status"]] += 1
        o["rows"].append(r)
    owner_list = sorted(owners.values(), key=lambda o: (-o["open"], o["owner"]))

    open_rows = [r for r in rows if r["status"] == "open"]
    return {
        "summary": {
            "open": len(open_rows),
            "open_patients": len({r["digits"] for r in open_rows}),
            "returned": len(returned_rows),
            "oldest_open_hours": max([r["age_hours"] for r in open_rows], default=0),
            "within_7_days_open": sum(1 for r in open_rows
                                      if r["days_until"] is not None and 0 <= r["days_until"] <= 7),
            "journeys": len(journeys or []),
            "journeys_without_number": no_number,
            "calls_scanned": scanned,
            "booked_numbers_seen": len(events),
            "min_connected_seconds": min_connected,
        },
        "owners": owner_list,
        "rows": rows,
    }
