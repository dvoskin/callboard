"""Do-not-call dispositions, honored in the CRM.

Danny, 2026-10-15: "All those do not call should've been unsubscribed and
triggered correct flows in our CRM", then "Fix all of these and then figure
out a way to fix previous ones".

A RingCX rep's "DNC (Do Not Call)" disposition lands on the Zoho Call
(Outgoing_call_disposition). Through August about nine in ten of those people
ended with their deal on Unsubscribe and Contacts.Do_Not_Contact set. From
2026-09-02 that fell to about one deal in three and, by October, almost no
contacts. Do_Not_Contact is the flag the Zoho texting rules, salesTextGate and
the RingCX push read, so those people kept their cadence calls and texts. The
Calls rule "RingCX Dispositions to Deal" still moves some deals; it skips
Expired ones, deals created that day, people linked through the Contact only,
and numbers with no CRM record.

This module makes the CRM say what the rep heard, one person at a time:

  collect(coql_all, dnc_calls, match_number)  everything needed to decide
  decide(world)                                 one decision per person
  payloads(decisions, now_iso)                  the exact writes
  apply(put, plan)                              the writes, chunked, counted

Rules
  - Never on a patient. Any deal of theirs in a paid, won, contract or
    unknown stage, or an Active Journey: "review" -- a person decides.
  - Never over a newer yes. A deal created after the DNC, an inbound or missed
    call after it, or a later call whose disposition is a conversation
    (booked, quoted, reached): "review".
  - Otherwise "apply":
      Contact   Do_Not_Contact, the SMS and email opt-outs, Unsubscribed_Mode
                Manual and Unsubscribed_Time (only when empty); trigger [] --
                Contacts has no edit rule
      Deals     every open sales deal of the person -> Stage Unsubscribe,
                RC_SMS_Opt_Out on, every Send_*_SMS off -- trigger [], SILENT.
                Danny, 2026-10-16: "Do this silently without any outbound
                messages". With workflows the move also fired the Zoho Flow
                webhook "Deals_ZohoFlow_create contact", whose effect is not
                visible from here. The rules' one useful effect, the Contact's
                Deal_Stage mirror, is written directly; the backoffice opt-out
                sync polls Unsubscribe deals and still picks them up.
                A deal already on Unsubscribe or Duplicates / Junk keeps its
                stage and only gets the switches
      Calls     every Scheduled / Overdue call on the person or those deals ->
                Cancelled, trigger [] (the stage rule leaves the old cadence
                calls open: 78 of 84 on the deals it did move on 2026-10-15)

Nothing here reads a call's notes except to append the one-line reason to a
cancelled call. Names and numbers stay out of the logs.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

DNC = "DNC (Do Not Call)"

# Deals in these stages move to Unsubscribe. An allowlist: a stage nobody
# listed here is treated as a patient stage and goes to review.
MOVE_STAGES = frozenset({
    "New Deal", "Call Scheduled", "Quote Sent", "Expired", "ON HOLD", "WhatsApp", "DND",
    "New Deal/Folli", "New Deals - Hair Transplant", "New Hair / Weight Loss",
    "Qualification", "Qualified", "Voucher $199",
})
# Already where they belong: switches only, the stage stays.
KEEP_STAGES = frozenset({"Unsubscribe", "Duplicates / Junk"})
UNSUB = "Unsubscribe"

SEND_OFF = {"RC_SMS_Opt_Out": True, "Send_Welcome_SMS": False, "Send_No_Show_SMS": False,
            "Send_Photo_Request_SMS": False, "New_Deal_24H_SMS": False, "Short_Call_SMS": False}
DEAL_FIELDS = ("id, Stage, Contact_Name, Created_Time, RC_SMS_Opt_Out, Send_Welcome_SMS, Send_No_Show_SMS, "
               "Send_Photo_Request_SMS, New_Deal_24H_SMS, Short_Call_SMS")
CONTACT_FIELDS = ("id, Do_Not_Contact, ringcentralextensionabr__SMS_OPT_OUT, Email_Opt_Out, Unsubscribed_Time, "
                  "Created_Time, Deal_Stage")
CALL_FIELDS = ("id, Who_Id, What_Id, Call_Type, Call_Start_Time, Outgoing_call_disposition, Outgoing_Call_Status")
OPEN_CALL = ("Scheduled", "Overdue")

_NOT_REACHED = ("no answer", "voicemail", "voice mail", "answering machine", "machine", "busy", "bad number",
                "wrong number", "disconnected", "dropped", "no contact", "not reached", "fax", "hang up",
                "hung up", "invalid", "left message", "lvm", "vm", "no disposition", "bad connection",
                "don't hear", "audio issues", "connection issues", "test", "n/a")


def _t(s):
    """Zoho's ISO datetime -> aware datetime; None when blank or unreadable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())[-10:]


def number_of(call) -> str:
    """The dialled number of an unlinked RingCX call: its Subject reads
    "Outbound Call to +13055551234". Ten digits, or "" when there is none
    ("Inbound Call from anonymous caller")."""
    m = re.search(r"(\+?\d[\d\-\s().]{9,})", str(call.get("Subject") or ""))
    d = _digits(m.group(1)) if m else ""
    return d if len(d) == 10 else ""


def is_conversation(disposition) -> bool:
    """A later disposition that means a person talked to us: anything recorded
    that is not a do-not-call, not a no-reach and not a non-disposition."""
    r = (disposition or "").strip().lower()
    if not r or r.startswith("[no-agent-disp") or "timeout" in r:
        return False
    if "dnc" in r or "do not call" in r:
        return False
    return not any(w in r for w in _NOT_REACHED)


def _id(ref):
    return (ref or {}).get("id") if isinstance(ref, dict) else None


def collect(coql_all, dnc_calls, match_number=None, chunk=50):
    """Everything decide() needs, fetched in chunks of 50 ids (COQL's `in`
    limit). coql_all(query) returns every row of a query (it pages).
    match_number(digits) -> [contact rows] for unlinked calls (optional)."""
    world = {"dnc_calls": list(dnc_calls), "deals": {}, "contacts": {}, "journeys": {}, "calls": {},
             "numbers": {}}

    def by_ids(fields, module, field, ids, extra=""):
        rows = []
        ids = sorted(set(i for i in ids if i))
        for i in range(0, len(ids), chunk):
            part = ",".join(ids[i:i + chunk])
            where = f"{field} in ({part})"
            if extra:
                where = f"({where} and {extra})"
            rows += coql_all(f"select {fields} from {module} where {where}")
        return rows

    deal_ids = {_id(c.get("What_Id")) for c in dnc_calls if c.get("$se_module") == "Deals"}
    contact_ids = {_id(c.get("Who_Id")) for c in dnc_calls}
    contact_ids |= {_id(c.get("What_Id")) for c in dnc_calls if c.get("$se_module") == "Contacts"}
    for d in by_ids(DEAL_FIELDS, "Deals", "id", deal_ids):
        world["deals"][d["id"]] = d
        contact_ids.add(_id(d.get("Contact_Name")))
    if match_number:
        for c in dnc_calls:
            if c.get("$se_module") not in ("Deals", "Contacts") and not _id(c.get("Who_Id")):
                n = number_of(c)
                if n and n not in world["numbers"]:
                    rows = match_number(n) or []
                    world["numbers"][n] = [r["id"] for r in rows]
                    for r in rows:
                        world["contacts"][r["id"]] = r
                        contact_ids.add(r["id"])
    contact_ids.discard(None)
    for r in by_ids(CONTACT_FIELDS, "Contacts", "id", contact_ids):
        world["contacts"][r["id"]] = r
    for d in by_ids(DEAL_FIELDS, "Deals", "Contact_Name.id", contact_ids):
        world["deals"][d["id"]] = d
    for j in by_ids("id, Contact, Case_Status", "Journeys", "Contact.id", contact_ids, "Case_Status = 'Active'"):
        world["journeys"].setdefault(_id(j.get("Contact")), []).append(j["id"])
    for r in by_ids(CALL_FIELDS, "Calls", "Who_Id", contact_ids):
        world["calls"][r["id"]] = r
    for r in by_ids(CALL_FIELDS, "Calls", "What_Id", list(world["deals"].keys())):
        world["calls"][r["id"]] = r
    return world


def _people(world):
    """Group the DNC calls by person: a Contact id, else "deal:<id>" for a deal
    with no contact, else "num:<digits>" for a number with no CRM record."""
    people = {}
    for c in world["dnc_calls"]:
        keys = []
        cid = _id(c.get("Who_Id")) or (_id(c.get("What_Id")) if c.get("$se_module") == "Contacts" else None)
        did = _id(c.get("What_Id")) if c.get("$se_module") == "Deals" else None
        if not cid and did:
            cid = _id((world["deals"].get(did) or {}).get("Contact_Name"))
        if cid:
            keys = [cid]
        elif did:
            keys = ["deal:" + did]
        else:
            n = number_of(c)
            matched = world["numbers"].get(n) or []
            keys = list(matched) if matched else (["num:" + n] if n else [])
        for k in keys:
            p = people.setdefault(k, {"key": k, "calls": [], "at": None, "deal_ids": set()})
            p["calls"].append(c["id"])
            t = _t(c.get("Call_Start_Time"))
            if t and (p["at"] is None or t > p["at"]):
                p["at"] = t
            if did:
                p["deal_ids"].add(did)
            if not cid and not did:
                p["by_number"] = True
    return people


def decide(world):
    """One decision per person. Pure: reads the world, writes nothing."""
    out = []
    dnc_ids = {c["id"] for c in world["dnc_calls"]}
    deals_of = {}
    for d in world["deals"].values():
        deals_of.setdefault(_id(d.get("Contact_Name")), []).append(d)
    for key, p in sorted(_people(world).items()):
        dec = {"key": key, "dnc_calls": sorted(p["calls"]), "dnc_at": p["at"].isoformat() if p["at"] else None,
               "result": None, "reason": "", "contact": None, "deals_move": [], "deals_flag": [],
               "contact_write": False, "calls_cancel": []}
        out.append(dec)
        if key.startswith("num:"):
            dec.update(result="no_record", reason="no Contact has this number")
            continue
        if key.startswith("deal:"):
            cid = None
            deals = [world["deals"][key[5:]]] if key[5:] in world["deals"] else []
        else:
            cid = key
            dec["contact"] = cid
            deals = list({d["id"]: d for d in deals_of.get(cid, []) +
                          [world["deals"][i] for i in p["deal_ids"] if i in world["deals"]]}.values())
        con = world["contacts"].get(cid) if cid else None
        if cid and con is None:
            dec.update(result="review", reason="the Contact could not be read")
            continue
        if not deals and con is None:
            dec.update(result="review", reason="the deal could not be read")
            continue
        at = p["at"]
        # 1. a patient: never automated
        paid = sorted({d.get("Stage") or "(no stage)" for d in deals
                       if (d.get("Stage") or "") not in MOVE_STAGES | KEEP_STAGES})
        if paid:
            dec.update(result="review", reason="patient stage: " + ", ".join(paid))
            continue
        if cid and world["journeys"].get(cid):
            dec.update(result="review", reason="Active Journey")
            continue
        # 2. a newer yes: never overwritten
        if at is not None:
            newer = [d for d in deals if (_t(d.get("Created_Time")) or at) > at]
            if newer:
                dec.update(result="review", reason="a deal was created after the DNC")
                continue
            if con is not None and p.get("by_number") and (_t(con.get("Created_Time")) or at) > at:
                dec.update(result="review", reason="the Contact was created after the DNC")
                continue
            mine = {d["id"] for d in deals}
            later = [c for c in world["calls"].values()
                     if c["id"] not in dnc_ids and (_t(c.get("Call_Start_Time")) or at) > at
                     and ((cid and _id(c.get("Who_Id")) == cid) or _id(c.get("What_Id")) in mine)]
            if any((c.get("Call_Type") or "") in ("Inbound", "Missed") for c in later):
                dec.update(result="review", reason="they called in after the DNC")
                continue
            if any(is_conversation(c.get("Outgoing_call_disposition")) for c in later):
                dec.update(result="review", reason="a later call reached them")
                continue
        # 3. apply: only what is not already true
        for d in deals:
            stage = d.get("Stage") or ""
            if stage in MOVE_STAGES:
                dec["deals_move"].append(d["id"])
            elif any(d.get(k) != v for k, v in SEND_OFF.items()):
                dec["deals_flag"].append(d["id"])
        if con is not None and not (con.get("Do_Not_Contact") and con.get("ringcentralextensionabr__SMS_OPT_OUT")
                                    and con.get("Email_Opt_Out")):
            dec["contact_write"] = True
        # the Contact's copy of the deal stage, which the stage rule would
        # have written had the move run with workflows
        if con is not None and (dec["deals_move"] or any((d.get("Stage") or "") == UNSUB for d in deals)) \
                and con.get("Deal_Stage") != UNSUB:
            dec["contact_write"] = True
            dec["contact_stage"] = True
        mine = {d["id"] for d in deals}
        dec["calls_cancel"] = sorted(c["id"] for c in world["calls"].values()
                                     if (c.get("Outgoing_Call_Status") or "") in OPEN_CALL
                                     and ((cid and _id(c.get("Who_Id")) == cid) or _id(c.get("What_Id")) in mine))
        dec["deals_move"].sort()
        dec["deals_flag"].sort()
        busy = dec["deals_move"] or dec["deals_flag"] or dec["contact_write"] or dec["calls_cancel"]
        dec["result"] = "apply" if busy else "done"
        dec["reason"] = "" if busy else "already unsubscribed"
    return out


def payloads(decisions, world, now_iso):
    """The writes for every "apply" decision, deduped across people. The note
    on a cancelled call carries the DNC's own date only: the server's offset
    is not Eastern, and Zoho keeps the modified time anyway."""
    contacts, move, flag, cancel = {}, {}, {}, {}
    for dec in decisions:
        if dec["result"] != "apply":
            continue
        cid = dec.get("contact")
        if cid and dec["contact_write"]:
            con = world["contacts"].get(cid) or {}
            row = {"id": cid, "Do_Not_Contact": True, "ringcentralextensionabr__SMS_OPT_OUT": True,
                   "Email_Opt_Out": True}
            if not con.get("Unsubscribed_Time"):
                row.update({"Unsubscribed_Mode": "Manual", "Unsubscribed_Time": now_iso})
            if dec.get("contact_stage"):
                row["Deal_Stage"] = UNSUB
            contacts[cid] = row
        for i in dec["deals_move"]:
            move[i] = dict({"id": i, "Stage": UNSUB}, **SEND_OFF)
        for i in dec["deals_flag"]:
            if i not in move:
                flag[i] = dict({"id": i}, **SEND_OFF)
        when = (dec.get("dnc_at") or "")[:10]
        for i in dec["calls_cancel"]:
            cancel[i] = {"id": i, "Outgoing_Call_Status": "Cancelled",
                         "_note": "Cancelled: do not call (DNC disposition %s)." % when}
    return {"contacts": list(contacts.values()), "deals_move": list(move.values()),
            "deals_flag": list(flag.values()), "calls_cancel": list(cancel.values())}


def apply(put, plan, describe=None, log=None):
    """Run the writes. put(module, rows, trigger) -> [(id, ok, detail)].
    describe(ids) -> {id: Description} so a cancelled call keeps its notes
    and gains one line. Returns counts, the ids written and the failures."""
    res = {"contacts": 0, "deals_moved": 0, "deals_flagged": 0, "calls_cancelled": 0, "errors": [],
           "ids": {"contacts": [], "deals_moved": [], "deals_flagged": [], "calls_cancelled": []}}
    calls = plan["calls_cancel"]
    if calls and describe:
        notes = {}
        for i in range(0, len(calls), 50):
            notes.update(describe([c["id"] for c in calls[i:i + 50]]) or {})
        calls = [{"id": c["id"], "Outgoing_Call_Status": "Cancelled",
                  "Description": ((notes.get(c["id"]) or "").rstrip() + "\n\n" + c["_note"]).strip()} for c in calls]
    else:
        calls = [{"id": c["id"], "Outgoing_Call_Status": "Cancelled"} for c in calls]
    steps = (("Contacts", plan["contacts"], [], "contacts"),
             ("Deals", plan["deals_move"], [], "deals_moved"),
             ("Deals", plan["deals_flag"], [], "deals_flagged"),
             ("Calls", calls, [], "calls_cancelled"))
    for module, rows, trigger, name in steps:
        for i in range(0, len(rows), 100):
            part = rows[i:i + 100]
            try:
                results = put(module, part, trigger)
            except Exception as e:  # noqa: BLE001
                res["errors"].append({"module": module, "ids": [r["id"] for r in part],
                                      "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
                continue
            for rid, ok, detail in results:
                if ok:
                    res[name] += 1
                    res["ids"][name].append(rid)
                else:
                    res["errors"].append({"module": module, "id": rid, "detail": str(detail)[:160]})
    if log and res["errors"]:
        log.warning("dnc apply: %d write errors", len(res["errors"]))
    return res


def summarize(decisions):
    """Counts only -- safe to print and to keep."""
    by = {}
    for d in decisions:
        k = d["result"] if d["result"] != "review" else "review: " + d["reason"]
        by[k] = by.get(k, 0) + 1
    return {"people": len(decisions), "by_result": dict(sorted(by.items())),
            "deals_move": sum(len(d["deals_move"]) for d in decisions if d["result"] == "apply"),
            "deals_flag": sum(len(d["deals_flag"]) for d in decisions if d["result"] == "apply"),
            "contacts": sum(1 for d in decisions if d["result"] == "apply" and d["contact_write"]),
            "calls_cancel": sum(len(d["calls_cancel"]) for d in decisions if d["result"] == "apply")}


class Store:
    """The ledger on disk: every DNC call id already handled, the people sent
    to review, the runs. {calls: {id: {result, at, person}}, review: {key: {...}},
    runs: [...], armed: bool}"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = {"calls": {}, "review": {}, "runs": [], "armed": False}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data.update({k: v for k, v in d.items() if k in ("calls", "review", "runs", "armed", "backfill")})
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            self.data["load_error"] = "%s: %s" % (type(e).__name__, e)
        return self.data

    def save(self):
        tmp = str(self.path) + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=0, sort_keys=True)
            os.replace(tmp, str(self.path))

    def pending(self, dnc_calls):
        """The DNC calls not yet settled. A call whose person failed to write
        stays pending and is tried again on the next run."""
        done = self.data["calls"]
        return [c for c in dnc_calls if (done.get(c["id"]) or {}).get("result") not in
                ("applied", "done", "review", "no_record")]

    def record(self, decisions, res, now_iso):
        failed = {e.get("id") for e in res.get("errors") or []}
        for e in res.get("errors") or []:
            failed.update(e.get("ids") or [])
        with self._lock:
            for d in decisions:
                ids = set(d["deals_move"]) | set(d["deals_flag"]) | set(d["calls_cancel"])
                if d.get("contact") and d["contact_write"]:
                    ids.add(d["contact"])
                result = d["result"]
                if result == "apply":
                    result = "error" if ids & failed else "applied"
                for cid in d["dnc_calls"]:
                    self.data["calls"][cid] = {"result": result, "at": now_iso, "person": d["key"],
                                               "reason": d["reason"] or None}
                if d["result"] == "review":
                    self.data["review"][d["key"]] = {"reason": d["reason"], "dnc_at": d["dnc_at"],
                                                     "calls": d["dnc_calls"], "seen": now_iso}
                elif d["key"] in self.data["review"] and result in ("applied", "done"):
                    self.data["review"].pop(d["key"], None)

    def note_run(self, info):
        with self._lock:
            self.data["runs"] = (self.data["runs"] + [info])[-40:]
