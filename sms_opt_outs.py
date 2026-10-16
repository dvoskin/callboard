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

KEYWORDS = ("stop", "stopall", "unsubscribe", "cancel", "end", "quit")
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
    if words[0] in KEYWORDS and (len(words) == 1 or words[0] in ("stop", "stopall", "unsubscribe")):
        return True
    if len(words) <= 4 and ("stop" in words or "unsubscribe" in words) and not (set(words) & _NEGATION):
        return True
    return False


def scan(fetch_messages, seats, start_dt, end_dt, max_pages=4, timeout=15.0, pause=0.3):
    """Every opt-out text on these seats in [start_dt, end_dt].

    fetch_messages -- RingCXClient.fetch_extension_messages (keep_text is asked for)
    seats          -- [{name, ext, ext_id}, ...]
    Returns (hits, problems): hits are dicts {digits, seat, ext, at, text,
    msg_id}; problems are per-seat notes (a seat whose store could not be read
    is reported, never counted as clean).
    """
    hits, problems = [], []
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
    return hits, problems


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
                self.data = {"numbers": d["numbers"], "runs": d.get("runs") or []}
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


def mark_in_zoho(base_url, headers_fn, digits, now_iso, http, log=None):
    """Set the org's opt-out fields for one number, everywhere it appears.

    Contact and Lead: ringcentralextensionabr__SMS_OPT_OUT, Unsubscribed_Mode
    = Manual, Unsubscribed_Time. Deal (through its Contact): RC_SMS_Opt_Out
    and every Send_*_SMS switch off. All with trigger: []. Returns a dict of
    counts and errors; raises nothing.
    """
    out = {"contacts": 0, "leads": 0, "deals": 0, "errors": [], "at": now_iso}

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
        r = http.put(f"{base_url}/crm/v6/{module}", headers=headers_fn(), json={"data": data, "trigger": []}, timeout=30)
        body = r.json() if r.content else {}
        ok = sum(1 for x in (body.get("data") or []) if x.get("code") == "SUCCESS")
        bad = [x for x in (body.get("data") or []) if x.get("code") != "SUCCESS"]
        if not r.ok or bad:
            out["errors"].append({"module": module, "http": r.status_code, "detail": str(bad or body)[:160]})
        return ok

    try:
        where = f"(Phone like '%{digits}' or Mobile like '%{digits}')"
        contacts = [r["id"] for r in coql(f"select id, Phone, Mobile from Contacts where {where} limit 50") if _same(r)]
        leads = [r["id"] for r in coql(f"select id, Phone, Mobile from Leads where {where} limit 50") if _same(r)]
        deals = []
        if contacts:
            deals = [r["id"] for r in coql("select id from Deals where Contact_Name.id in (%s) limit 100" % ",".join(contacts))]
        stamp = {"ringcentralextensionabr__SMS_OPT_OUT": True, "Unsubscribed_Mode": "Manual", "Unsubscribed_Time": now_iso}
        if contacts:
            out["contacts"] = put("Contacts", [dict(id=i, **stamp) for i in contacts])
        if leads:
            out["leads"] = put("Leads", [dict(id=i, **stamp) for i in leads])
        if deals:
            off = {"RC_SMS_Opt_Out": True, "Send_Welcome_SMS": False, "Send_No_Show_SMS": False,
                   "Send_Photo_Request_SMS": False, "New_Deal_24H_SMS": False, "Short_Call_SMS": False}
            out["deals"] = put("Deals", [dict(id=i, **off) for i in deals])
        out["found"] = {"contacts": len(contacts), "leads": len(leads), "deals": len(deals)}
    except Exception as e:  # noqa: BLE001
        out["errors"].append({"module": "lookup", "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
        if log:
            log.warning("opt-out zoho mark failed for ...%s: %s", digits[-4:], e)
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
