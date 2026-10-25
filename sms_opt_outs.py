"""STOP replies, honored.

Danny, 2026-10-15: "make sure all stop replies are getting honored". A STOP
sent to a rep's or coordinator's own RingEX extension is processed by nothing:
the extension is not a campaign number, so no platform keeps an opt-out list
for it. Fourteen STOPs arrived on the sales floor in one day and every one
sat in a rep's inbox as an unread text.

This module is the list and the two things that keep it true:

  is_opt_out(text)      the CTIA keywords (STOP, STOPALL, UNSUBSCRIBE, CANCEL,
                        END, QUIT) at the start of a text, the stop-sign emoji,
                        and short "please stop" shapes -- never "don't stop"
                        and never a question
  scan(...)             every inbound on every texting extension, last N days
  Store                 the list on disk (/data on Render), deduped by message
  mark_in_zoho(...)     the org's own opt-out fields on the Contact, the Lead
                        and the Deal, written with trigger: [] so no workflow
                        fires on the edit; the Deal's stage is NOT changed --
                        a STOP is about texts, not about the sale

Nothing here reads a text body except to decide whether it is a STOP; what is
kept is the first 40 characters of the matching text, the number, the seat and
the time.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone

# "stp" is a thumbed STOP: seen 2026-10-24 on Salome's line ("STP"), unmarked
# until a person read the thread (Danny, 2026-10-25: "fix these").
KEYWORDS = ("stop", "stopall", "stp", "unsubscribe", "cancel", "end", "quit")
_NEGATION = {"dont", "don", "not", "never", "cant", "can", "wont", "won", "didnt", "didn", "no"}
_STOP_SIGN = "\U0001F6D1"


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())[-10:]


def normalize(text) -> str:
    t = str(text or "").replace(_STOP_SIGN, " stop ").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return " ".join(t.split())


def is_opt_out(text) -> bool:
    """True for a text that is an opt-out request.

    "STOP", "Stop ", "STOP!", "stop texting me", "unsubscribe", "please stop",
    "pls stop", "🛑" -> True.  "don't stop", "stop by the office?", "I can't
    stop thinking about it", "Okay", "Yes" -> False.
    """
    raw = str(text or "")
    if "?" in raw:
        return False
    t = normalize(raw)
    if not t:
        return False
    words = t.split()
    if words[0] in KEYWORDS and (len(words) == 1 or words[0] in ("stop", "stopall", "stp", "unsubscribe")):
        return True
    if len(words) <= 4 and ("stop" in words or "unsubscribe" in words) and not (set(words) & _NEGATION):
        return True
    return False


def scan(fetch_messages, seats, start_dt, end_dt, max_pages=4, timeout=15.0, pause=0.3):
    """Every opt-out text on these seats in [start_dt, end_dt].

    fetch_messages -- RingCXClient.fetch_extension_messages (keep_text is asked for)
    seats          -- [{name, ext, ext_id}, ...]
    Returns (hits, problems, outbound): hits are dicts {digits, seat, ext,
    at, text, msg_id}; problems are per-seat notes (a seat whose store could
    not be read is reported, never counted as clean); outbound is every text
    the seats SENT, as {digits, seat, at, status} -- what tells a STOP that was
    honored from one that was not.
    """
    hits, problems, outbound = [], [], []
    for seat in seats:
        try:
            rows, meta = fetch_messages(seat["ext_id"], start_dt, end_dt, max_pages=max_pages,
                                        max_wait=0.0, timeout=timeout, keep_text=True)
        except Exception as e:  # noqa: BLE001
            problems.append({"seat": seat.get("name"), "note": "%s: %s" % (type(e).__name__, str(e)[:120])})
            continue
        if meta.get("note"):
            problems.append({"seat": seat.get("name"), "note": meta.get("note")})
        for r in rows:
            if r.get("direction") == "Outbound":
                to = _digits(r.get("to_number"))
                if len(to) == 10:
                    outbound.append({"digits": to, "seat": seat.get("name"), "at": r.get("start_time") or "",
                                     "status": r.get("status") or ""})
                continue
            if r.get("direction") != "Inbound":
                continue
            txt = r.get("text", "")
            if not is_opt_out(txt):
                continue
            num = _digits(r.get("from_number"))
            if len(num) != 10:
                continue
            hits.append({"digits": num, "seat": seat.get("name"), "ext": str(seat.get("ext") or ""),
                         "at": r.get("start_time") or "", "text": txt.strip()[:40], "msg_id": str(r.get("id") or "")})
        if pause:
            time.sleep(pause)
    return hits, problems, outbound


def texted_after_stop(numbers, outbound):
    """Every text SENT to a number after it said STOP, from any seat.

    numbers  -- the Store's {digits: {first_seen, ...}}
    outbound -- scan()'s third return
    A failed send still counts: it was attempted. Sorted newest first.
    """
    out = []
    for o in outbound:
        rec = numbers.get(o["digits"])
        if rec and o["at"] > (rec.get("first_seen") or "9999"):
            out.append(dict(o))
    out.sort(key=lambda o: o["at"], reverse=True)
    return out


class Store:
    """The opt-out list on disk: {numbers: {digits: {...}}, runs: [...]}"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = {"numbers": {}, "runs": []}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("numbers"), dict):
                # zoho_armed too: dropping it here disarmed the Zoho marking on
                # every restart (found 2026-10-16 -- armed 10-15 21:51, false
                # after the next deploy).
                self.data = {"numbers": d["numbers"], "runs": d.get("runs") or []}
                if "zoho_armed" in d:
                    self.data["zoho_armed"] = bool(d["zoho_armed"])
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            self.data.setdefault("load_error", "%s: %s" % (type(e).__name__, e))
        return self.data

    def save(self):
        tmp = str(self.path) + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            import os
            os.replace(tmp, str(self.path))

    def add(self, hits):
        """Fold hits in; returns the digits that are NEW to the list."""
        new = []
        with self._lock:
            nums = self.data["numbers"]
            for h in hits:
                rec = nums.get(h["digits"])
                if rec is None:
                    rec = {"first_seen": h["at"], "last_seen": h["at"], "seat": h["seat"], "ext": h["ext"],
                           "text": h["text"], "msg_ids": [], "zoho": None}
                    nums[h["digits"]] = rec
                    new.append(h["digits"])
                if h["msg_id"] and h["msg_id"] not in rec["msg_ids"]:
                    rec["msg_ids"].append(h["msg_id"])
                if h["at"] > (rec.get("last_seen") or ""):
                    rec["last_seen"], rec["seat"], rec["ext"], rec["text"] = h["at"], h["seat"], h["ext"], h["text"]
                if h["at"] < (rec.get("first_seen") or h["at"]):
                    rec["first_seen"] = h["at"]
        return new

    def digits(self):
        return sorted(self.data["numbers"].keys())

    def note_run(self, info):
        with self._lock:
            self.data["runs"] = (self.data["runs"] + [info])[-30:]


# A STOP is a full unsubscribe (Danny, 2026-10-25: "when it marks it stops it
# moves all active records to DNC stages and prevents texting again"). Deals in
# these stages stay where they are: a paid patient's deal is not a sales deal
# and the surgical side still needs it. Their SMS switches go off all the same.
STOP_KEEP_STAGES = frozenset({"Unsubscribe", "Duplicates / Junk", "Closed Won - Surgery Scheduled",
                              "Payment Received", "Payment Recieved"})
STOP_UNSUB = "Unsubscribe"
STOP_SEND_OFF = {"RC_SMS_Opt_Out": True, "Send_Welcome_SMS": False, "Send_No_Show_SMS": False,
                 "Send_Photo_Request_SMS": False, "New_Deal_24H_SMS": False, "Short_Call_SMS": False}


def mark_in_zoho(base_url, headers_fn, digits, now_iso, http, log=None):
    """A STOP, honored everywhere the number appears -- the whole unsubscribe,
    not just the SMS flags (the flags alone left deals open and calls booked
    until 2026-10-25). All with trigger: [] so no workflow texts fire.

      Contact   Do_Not_Contact, ringcentralextensionabr__SMS_OPT_OUT,
                Email_Opt_Out, Unsubscribed_Mode = Manual, Unsubscribed_Time,
                Deal_Stage mirror = Unsubscribe when a deal moved.
                A Contact with an Active Journey keeps Do_Not_Contact OFF: the
                coordinator still has to reach a booked patient by phone. Her
                texts stop through the Journey's own switches instead.
      Lead      the SMS opt-out and the Unsubscribed stamp.
      Deals     found through the Contact AND by the deal's own Phone: every
                deal not in STOP_KEEP_STAGES -> Stage Unsubscribe; RC_SMS_Opt_Out
                on and every Send_*_SMS switch off on ALL of them, kept or moved.
      Journeys  every Active Journey of the Contact: Send_SMS off,
                Hold_Send_SMS_Follow_Up on.
      Calls     every Scheduled / Overdue call on the Contact or its deals ->
                Cancelled, with a line in its Description saying why.
    Returns counts, what was found, and errors; raises nothing. Each step is
    independent: one failed write is reported, the rest still happen."""
    out = {"contacts": 0, "leads": 0, "deals": 0, "deals_moved": 0, "journeys": 0, "calls_cancelled": 0,
           "errors": [], "at": now_iso, "full": True}       # full: the whole unsubscribe, not the flags alone

    def _same(rec):
        # The query matched the ten digits somewhere in the field; keep only a
        # field whose digits END in them ("+1 305...", "1305...", "(305) ...").
        # Never the last seven alone: another area code shares them.
        return any("".join(ch for ch in str(rec.get(f) or "") if ch.isdigit()).endswith(digits)
                   for f in ("Phone", "Mobile"))

    def coql(q):
        r = http.post(f"{base_url}/crm/v6/coql", headers=headers_fn(), json={"select_query": q}, timeout=25)
        if r.status_code == 204:
            return []
        if not r.ok:
            raise RuntimeError("COQL HTTP %d %s" % (r.status_code, (r.text or "")[:120]))
        return (r.json() or {}).get("data") or []

    def put(module, data):
        if not data:
            return 0
        ok = 0
        for i in range(0, len(data), 100):
            part = data[i:i + 100]
            try:
                r = http.put(f"{base_url}/crm/v6/{module}", headers=headers_fn(), json={"data": part, "trigger": []}, timeout=30)
                body = r.json() if r.content else {}
            except Exception as e:  # noqa: BLE001
                out["errors"].append({"module": module, "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
                continue
            ok += sum(1 for x in (body.get("data") or []) if x.get("code") == "SUCCESS")
            bad = [x for x in (body.get("data") or []) if x.get("code") != "SUCCESS"]
            if not r.ok or bad:
                out["errors"].append({"module": module, "http": r.status_code, "detail": str(bad or body)[:160]})
        return ok

    try:
        where = f"(Phone like '%{digits}' or Mobile like '%{digits}')"
        contacts = [r["id"] for r in coql(f"select id, Phone, Mobile from Contacts where {where} limit 50") if _same(r)]
        leads = [r["id"] for r in coql(f"select id, Phone, Mobile from Leads where {where} limit 50") if _same(r)]
        deals = {}
        if contacts:
            for r in coql("select id, Stage from Deals where Contact_Name.id in (%s) limit 100" % ",".join(contacts)):
                deals[r["id"]] = r
        for r in coql(f"select id, Stage, Phone from Deals where Phone like '%{digits}' limit 100"):
            if "".join(ch for ch in str(r.get("Phone") or "") if ch.isdigit()).endswith(digits):
                deals.setdefault(r["id"], r)
        journeys, booked = [], set()
        if contacts:
            for r in coql("select id, Contact from Journeys where (Contact.id in (%s) and Case_Status = 'Active') limit 200"
                          % ",".join(contacts)):
                journeys.append(r["id"])
                booked.add((r.get("Contact") or {}).get("id"))
        out["found"] = {"contacts": len(contacts), "leads": len(leads), "deals": len(deals), "journeys": len(journeys)}
    except Exception as e:  # noqa: BLE001
        out["errors"].append({"module": "lookup", "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
        if log:
            log.warning("opt-out zoho mark failed for ...%s: %s", digits[-4:], e)
        return out

    move = [i for i, d in deals.items() if (d.get("Stage") or "") not in STOP_KEEP_STAGES]
    stamp = {"ringcentralextensionabr__SMS_OPT_OUT": True, "Email_Opt_Out": True,
             "Unsubscribed_Mode": "Manual", "Unsubscribed_Time": now_iso}
    if contacts:
        rows = []
        for i in contacts:
            row = dict(id=i, **stamp)
            if i not in booked:
                row["Do_Not_Contact"] = True
            if move:
                row["Deal_Stage"] = STOP_UNSUB          # the Contact's mirror of the stage
            rows.append(row)
        out["contacts"] = put("Contacts", rows)
        out["do_not_contact"] = sum(1 for i in contacts if i not in booked)
    if leads:
        out["leads"] = put("Leads", [dict(id=i, ringcentralextensionabr__SMS_OPT_OUT=True, Unsubscribed_Mode="Manual",
                                          Unsubscribed_Time=now_iso) for i in leads])
    if deals:
        rows = [dict({"id": i}, **STOP_SEND_OFF, **({"Stage": STOP_UNSUB} if i in move else {})) for i in deals]
        out["deals"] = put("Deals", rows)
        out["deals_moved"] = len(move)
    if journeys:
        out["journeys"] = put("Journeys", [{"id": i, "Send_SMS": False, "Hold_Send_SMS_Follow_Up": True} for i in journeys])
    # open calls on the person or any of the deals: cancelled, and said why
    try:
        calls = []
        if contacts:
            calls += coql("select id, Outgoing_Call_Status, Description from Calls where (Who_Id in (%s) and Outgoing_Call_Status in ('Scheduled','Overdue')) limit 200"
                          % ",".join(contacts))
        if deals:
            ids = list(deals)
            for i in range(0, len(ids), 50):
                calls += coql("select id, Outgoing_Call_Status, Description from Calls where (What_Id in (%s) and Outgoing_Call_Status in ('Scheduled','Overdue')) limit 200"
                              % ",".join(ids[i:i + 50]))
        seen, rows = set(), []
        for c in calls:
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            rows.append({"id": c["id"], "Outgoing_Call_Status": "Cancelled",
                         "Description": ((c.get("Description") or "").rstrip() + "\n\nCancelled: patient texted STOP (%s)." % now_iso[:10]).strip()})
        out["calls_cancelled"] = put("Calls", rows)
        out["found"]["calls"] = len(rows)
    except Exception as e:  # noqa: BLE001
        out["errors"].append({"module": "Calls", "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
    if out["errors"] and log:
        log.warning("opt-out zoho mark for ...%s had errors: %s", digits[-4:], out["errors"][:3])
    return out


def now_iso_local(tz_offset_minutes: int) -> str:
    """Zoho's datetime shape, in the org's offset: 2026-10-15T21:30:00-04:00."""
    off = -int(tz_offset_minutes)
    sign = "+" if off >= 0 else "-"
    hh, mm = divmod(abs(off), 60)
    t = datetime.now(timezone.utc).replace(microsecond=0)
    from datetime import timedelta
    local = t + timedelta(minutes=off)
    return local.strftime("%Y-%m-%dT%H:%M:%S") + f"{sign}{hh:02d}:{mm:02d}"
