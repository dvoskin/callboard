"""Open tasks and scheduled calls that no longer belong anywhere, deleted slowly.

Danny, 2026-10-19: "delete any open reminders and tasks on completely
unrelated records" ... "without exhausting crazy API limits". On that day the
CRM held 188,000 open tasks and 468,000 overdue scheduled calls; 171,000 and
441,000 of them were more than three months past due.

Four kinds go, each open (a task not Completed, a call still Overdue) and due
before today:

  paid     on a deal whose retainer is paid -- the quote follow-ups and
           deposit chasers of a sale that is over
  dead     on a deal in Unsubscribe or Duplicates / Junk
  orphan   a task on no deal and no contact: due before today, or undated
           and created STALE_DAYS ago
  stale    anything due STALE_DAYS (90) or more ago

Never deleted, whatever the kind:
  - an "Intro Call - ..." task (it makes the Journey when completed)
  - anything on a Journey
  - anything owned by a coordinator, biller or pre-op (Team Schedules), or by
    a current scheduling owner (Yevgen Briks is not in Team Schedules)
  - anything due today or later, and every call still Scheduled

The work is split into passes, each read in id order (so deleting behind the
cursor never shifts a page) and resumed from its cursor after a restart:

  deal_tasks, deal_calls   due in the last STALE_DAYS, on a deal: paid / dead
  orphan_tasks             no deal, no contact
  stale_tasks, stale_calls due STALE_DAYS ago or more, on anything

A step reads one page and deletes what it decided, 100 ids a call, with
workflows off. Zoho prices a COQL read by its size (1 credit to 200 rows, 2 to
1,000, 3 to 2,000) and a delete at 1 credit per 100 ids, so a full page costs
23 credits. Steps stop at the day's credit cap and on the first 429.

Shipped DISARMED: nothing is deleted until a signed-in person arms it.
Counts only in the ledger and the logs -- no names, no subjects.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta

STALE_DAYS = 90
PAGE = 2000
DELETE_CHUNK = 100
DEAD_STAGES = frozenset({"Unsubscribe", "Duplicates / Junk"})
TASK_OPEN = "Status not in ('Completed', 'Complete')"
CALL_OPEN = "Outgoing_Call_Status = 'Overdue'"
TASK_FIELDS = "id, Subject, Owner, What_Id, Who_Id, $se_module, Due_Date, Created_Time"
CALL_FIELDS = "id, Subject, Owner, What_Id, Who_Id, $se_module, Call_Start_Time"

# (pass, module) in the order they run: the small, certain ones first.
PASSES = (("deal_tasks", "Tasks"), ("deal_calls", "Calls"), ("orphan_tasks", "Tasks"),
          ("stale_tasks", "Tasks"), ("stale_calls", "Calls"))


def cutoffs(today: date, zone: str) -> dict:
    """The fixed dates one sweep runs against. zone: "-04:00"."""
    cut = today - timedelta(days=STALE_DAYS)
    return {"today": today.isoformat(), "cut": cut.isoformat(),
            "today_t": "%sT00:00:00%s" % (today.isoformat(), zone),
            "cut_t": "%sT00:00:00%s" % (cut.isoformat(), zone)}


def query(pass_name: str, c: dict, cursor: str = "0", limit: int = PAGE) -> str:
    """One page of a pass, after `cursor`, in id order. COQL wants every pair
    of conditions in its own brackets."""
    after = "id > %s" % (cursor or "0")
    if pass_name == "deal_tasks":
        where = "(((%s and Due_Date >= '%s') and Due_Date < '%s') and %s)" % (TASK_OPEN, c["cut"], c["today"], after)
    elif pass_name == "deal_calls":
        where = "(((%s and Call_Start_Time >= '%s') and Call_Start_Time < '%s') and %s)" % (
            CALL_OPEN, c["cut_t"], c["today_t"], after)
    elif pass_name == "orphan_tasks":
        where = "(((%s and What_Id is null) and Who_Id is null) and %s)" % (TASK_OPEN, after)
    elif pass_name == "stale_tasks":
        where = "((%s and Due_Date < '%s') and %s)" % (TASK_OPEN, c["cut"], after)
    elif pass_name == "stale_calls":
        where = "((%s and Call_Start_Time < '%s') and %s)" % (CALL_OPEN, c["cut_t"], after)
    else:
        raise ValueError(pass_name)
    module = dict(PASSES)[pass_name]
    fields = TASK_FIELDS if module == "Tasks" else CALL_FIELDS
    return "select %s from %s where %s order by id limit %d" % (fields, module, where, limit)


def coql_credits(rows: int) -> int:
    return 1 if rows <= 200 else 2 if rows <= 1000 else 3


def _id(v):
    return str((v or {}).get("id") or "") if isinstance(v, dict) else str(v or "")


def _module(row) -> str:
    return str(row.get("$se_module") or "")


def deal_ids(rows) -> list:
    """The deals a page of the deal_* passes needs looked up."""
    return sorted({_id(r.get("What_Id")) for r in rows if _module(r) == "Deals" and _id(r.get("What_Id"))})


def decide(row: dict, pass_name: str, c: dict, protected: set, deals: dict | None = None):
    """("delete", kind) or ("keep", why). deals: {id: {"Stage", "Retainer_Paid"}}
    for the deal_* passes."""
    if str(row.get("Subject") or "").strip().lower().startswith("intro call"):
        return "keep", "intro call"
    if _module(row) == "Journeys":
        return "keep", "on a journey"
    if _id(row.get("Owner")) in protected:
        return "keep", "coordinator, biller or pre-op"
    due = str(row.get("Due_Date") or row.get("Call_Start_Time") or "")[:10]
    if due and due >= c["today"]:
        return "keep", "due today or later"
    if pass_name in ("stale_tasks", "stale_calls"):
        return ("delete", "stale") if due and due < c["cut"] else ("keep", "not stale")
    if pass_name == "orphan_tasks":
        if _id(row.get("What_Id")) or _id(row.get("Who_Id")):
            return "keep", "attached"
        created = str(row.get("Created_Time") or "")[:10]
        if due or (created and created < c["cut"]):
            return "delete", "orphan"
        return "keep", "undated and recent"
    # deal_tasks / deal_calls
    if not due:
        return "keep", "undated"
    if _module(row) != "Deals" or not _id(row.get("What_Id")):
        return "keep", "not on a deal"
    d = (deals or {}).get(_id(row.get("What_Id")))
    if not d:
        return "keep", "deal not found"
    if d.get("Retainer_Paid"):
        return "delete", "paid"
    if d.get("Stage") in DEAD_STAGES:
        return "delete", "dead"
    return "keep", "live deal"


class Store:
    """/data/task_sweep.json: {armed, sweep: {cutoffs, passes: {name: {cursor,
    done, read, deleted}}, started, finished}, totals: {kind: n},
    kept: {why: n}, credits: {day, used}, runs: [...], last_error}. Counts
    and record ids only."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = {"armed": False, "sweep": None, "totals": {}, "kept": {}, "credits": {"day": None, "used": 0},
                     "runs": [], "last_error": None}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data.update({k: v for k, v in d.items() if k in self.data})
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

    def start(self, c: dict, now_iso: str):
        self.data["sweep"] = {"cutoffs": c, "started": now_iso, "finished": None,
                              "passes": {p: {"cursor": "0", "done": False, "read": 0, "deleted": 0}
                                         for p, _ in PASSES}}

    def current(self):
        """(pass, module, state) still to do, or None when the sweep is done."""
        sw = self.data.get("sweep") or {}
        for p, m in PASSES:
            st = (sw.get("passes") or {}).get(p)
            if st and not st.get("done"):
                return p, m, st
        return None

    def spend(self, day: str, credits: int):
        cr = self.data["credits"]
        if cr.get("day") != day:
            cr.update({"day": day, "used": 0})
        cr["used"] = cr.get("used", 0) + credits

    def spent(self, day: str) -> int:
        cr = self.data["credits"]
        return cr.get("used", 0) if cr.get("day") == day else 0

    def count(self, bucket: str, key: str, n: int = 1):
        b = self.data[bucket]
        b[key] = b.get(key, 0) + n

    def note_run(self, info):
        self.data["runs"] = (self.data["runs"] + [info])[-40:]
