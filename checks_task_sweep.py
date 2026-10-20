"""The stale task and reminder sweep -- what is deleted, what is never
touched, and how slowly it goes.

Danny, 2026-10-19: "delete any open reminders and tasks on completely
unrelated records" ... "without exhausting crazy API limits".
Run: ./.venv/bin/python -B checks_task_sweep.py   (tally LAST)
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import task_sweep as ts  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


TODAY = date(2026, 10, 19)
C = ts.cutoffs(TODAY, "-04:00")
ck("the cut is 90 days back, in the office's zone",
   C == {"today": "2026-10-19", "cut": "2026-07-21", "today_t": "2026-10-19T00:00:00-04:00",
         "cut_t": "2026-07-21T00:00:00-04:00"}, C)

# ---- the queries -------------------------------------------------------------
Q = {p: ts.query(p, C, "5212466000000000001") for p, _ in ts.PASSES}
for p, q in Q.items():
    ck("%s: in id order after the cursor, a page at a time" % p,
       "id > 5212466000000000001" in q and q.endswith("order by id limit 2000"), q)
    bare = re.sub(r"'[^']*'", "''", q)
    ck("%s: brackets balance, one pair per two conditions" % p, bare.count("(") == bare.count(")")
       and bare.split(" where ")[1].count(" and ") <= bare.split(" where ")[1].count("(") - bare.count("in ("), q)
for p in ("deal_tasks", "orphan_tasks", "stale_tasks"):
    ck("%s: open tasks only (a typo'd Complete counts as done)" % p,
       " from Tasks where " in Q[p] and "Status not in ('Completed', 'Complete')" in Q[p], Q[p])
for p in ("deal_calls", "stale_calls"):
    ck("%s: overdue calls only, never Scheduled ones" % p,
       " from Calls where " in Q[p] and "Outgoing_Call_Status = 'Overdue'" in Q[p] and "Scheduled" not in Q[p], Q[p])
ck("deal_tasks: due in the last 90 days, before today",
   "Due_Date >= '2026-07-21'" in Q["deal_tasks"] and "Due_Date < '2026-10-19'" in Q["deal_tasks"], Q["deal_tasks"])
ck("deal_calls: the same window", "Call_Start_Time >= '2026-07-21T00:00:00-04:00'" in Q["deal_calls"]
   and "Call_Start_Time < '2026-10-19T00:00:00-04:00'" in Q["deal_calls"], Q["deal_calls"])
ck("orphan_tasks: no deal and no contact", "What_Id is null" in Q["orphan_tasks"] and "Who_Id is null" in Q["orphan_tasks"])
ck("stale_tasks / stale_calls: 90 days past due", "Due_Date < '2026-07-21'" in Q["stale_tasks"]
   and "Call_Start_Time < '2026-07-21T00:00:00-04:00'" in Q["stale_calls"])
ck("every query reads what decide needs", all(f in Q["stale_tasks"] for f in ("Subject", "Owner", "$se_module", "Due_Date"))
   and "Call_Start_Time" in Q["stale_calls"].split(" from ")[0])
ck("COQL credits by page size", [ts.coql_credits(n) for n in (0, 200, 201, 1000, 1001, 2000)] == [1, 1, 2, 2, 3, 3])

# ---- decide --------------------------------------------------------------------
KEEP = {"u-coord", "u-biller"}
DEALS = {"d-paid": {"id": "d-paid", "Stage": "Closed Won - Surgery Scheduled", "Retainer_Paid": True},
         "d-unsub": {"id": "d-unsub", "Stage": "Unsubscribe", "Retainer_Paid": False},
         "d-junk": {"id": "d-junk", "Stage": "Duplicates / Junk", "Retainer_Paid": False},
         "d-live": {"id": "d-live", "Stage": "Quote Sent", "Retainer_Paid": False},
         "d-paid-unsub": {"id": "d-paid-unsub", "Stage": "Unsubscribe", "Retainer_Paid": True}}


def task(i="t1", subject="Quote Follow Up 3 - 1 Week", owner="u-rep", what=None, mod="Deals", who=None,
         due="2026-09-01", created="2026-08-20T10:00:00-04:00"):
    return {"id": i, "Subject": subject, "Owner": {"id": owner}, "What_Id": {"id": what} if what else None,
            "Who_Id": {"id": who} if who else None, "$se_module": mod if (what or who) else "Home",
            "Due_Date": due, "Created_Time": created}


ADMIN, RCC = "5212466000000357001", "5212466000078931001"


def call(i="c1", owner="u-rep", what="d-paid", start="2026-09-01T10:00:00-04:00", mod="Deals", by=ADMIN,
         subject="Quote Follow Up Call", desc=None):
    """An automatic call by default (Zoho Admin made it); by="u-rep" is one a
    person booked."""
    return {"id": i, "Subject": subject, "Owner": {"id": owner}, "Created_By": {"id": by}, "What_Id": {"id": what},
            "Who_Id": {"id": "k1"}, "$se_module": mod, "Call_Start_Time": start, "Description": desc}


dd = lambda row, p="deal_tasks": ts.decide(row, p, C, KEEP, DEALS)
ck("a sales follow-up on a paid patient: deleted as paid", dd(task(what="d-paid")) == ("delete", "paid"))
ck("... on Unsubscribe and on Junk: deleted as dead", dd(task(what="d-unsub")) == ("delete", "dead")
   and dd(task(what="d-junk")) == ("delete", "dead"))
ck("paid wins over Unsubscribe", dd(task(what="d-paid-unsub")) == ("delete", "paid"))
ck("a live sales deal: kept", dd(task(what="d-live")) == ("keep", "live deal"))
ck("an overdue call on a paid patient: deleted", dd(call(), "deal_calls") == ("delete", "paid"))
ck("a deal the lookup did not return: kept", dd(task(what="d-gone")) == ("keep", "deal not found"))
ck("on a contact only, in the deal window: kept", dd(task(what=None, who="k1", mod="Contacts")) == ("keep", "not on a deal"))
ck("undated, in the deal window: kept", dd(task(what="d-paid", due=None)) == ("keep", "undated"))
for label, row in (("an Intro Call task", task(subject="Intro Call - Pat · Deal Oct 19", what="d-paid")),
                   ("anything on a Journey", task(what="j1", mod="Journeys")),
                   ("a coordinator's task", task(owner="u-coord", what="d-paid")),
                   ("a biller's task", task(owner="u-biller", what="d-unsub")),
                   ("a task due today", task(what="d-paid", due="2026-10-19")),
                   ("a task due next week", task(what="d-paid", due="2026-10-26"))):
    for p in ("deal_tasks", "stale_tasks", "orphan_tasks"):
        ck("never deleted: %s (%s)" % (label, p), dd(row, p)[0] == "keep", dd(row, p))
ck("never deleted: a coordinator's overdue call", dd(call(owner="u-coord", start="2025-01-01T09:00:00-05:00"),
                                                     "stale_calls")[0] == "keep")
ck("stale: due before the cut, on anything", dd(task(what="d-live", due="2026-07-20"), "stale_tasks") == ("delete", "stale")
   and dd(task(what=None, who="k1", mod="Contacts", due="2024-01-05"), "stale_tasks") == ("delete", "stale"))
ck("stale: a call before the cut", dd(call(what="d-live", start="2026-07-20T23:00:00-04:00"), "stale_calls") == ("delete", "stale"))
ck("stale: on the cut day itself is not stale", dd(task(what="d-live", due="2026-07-21"), "stale_tasks") == ("keep", "not stale"))
ck("orphan: due before today", dd(task(due="2026-10-18"), "orphan_tasks") == ("delete", "orphan"))
ck("orphan: undated and 90+ days old", dd(task(due=None, created="2025-03-01T09:00:00-05:00"), "orphan_tasks")
   == ("delete", "orphan"))
ck("orphan: undated and recent is someone's to-do", dd(task(due=None, created="2026-10-01T09:00:00-04:00"),
                                                       "orphan_tasks") == ("keep", "undated and recent"))
ck("orphan: undated with no created time is kept", dd(task(due=None, created=None), "orphan_tasks")
   == ("keep", "undated and recent"))
ck("orphan: attached after all is kept", dd(task(who="k1", mod="Contacts"), "orphan_tasks") == ("keep", "attached"))
# Hand-booked calls are the paper trail: cancelled with a note, not deleted.
MAN = dict(by="u-rep", subject="Call scheduled with Pat")
ck("a call a person booked, on a paid patient: cancelled, not deleted", dd(call(**MAN), "deal_calls") == ("cancel", "paid"))
ck("... on a dead deal and when stale: cancelled too", dd(call(what="d-unsub", **MAN), "deal_calls") == ("cancel", "dead")
   and dd(call(what="d-live", start="2025-03-01T09:00:00-05:00", **MAN), "stale_calls") == ("cancel", "stale"))
ck("a call the RingCX dialer made: deleted", dd(call(by=RCC, subject="Dialer Call - Pat"), "deal_calls") == ("delete", "paid"))
for subj in ("Quote Follow Up 2 - 3 Days", "Dialer Call - Pat", "Scheduled Call: Automated", "Scheduled Call - Best Contact Time",
             "Follow-up call"):
    ck("an automation's subject is automatic whoever owns it: %s" % subj,
       dd(call(by="u-rep", subject=subj), "deal_calls") == ("delete", "paid"), dd(call(by="u-rep", subject=subj), "deal_calls"))
ck("a hand-booked call keeps every never-touch rule", dd(call(owner="u-coord", **MAN), "deal_calls") == ("keep", "coordinator, biller or pre-op")
   and dd(call(what="d-live", **MAN), "deal_calls") == ("keep", "live deal")
   and dd(call(start="2026-10-20T10:00:00-04:00", **MAN), "deal_calls") == ("keep", "due today or later"))
ck("a task a person typed is still deleted, never cancelled", dd(task(what="d-paid", subject="call her back")) == ("delete", "paid"))
ck("no creator read: a person booked it", ts.manual_call({"Subject": "Call with Pat"}) and not ts.manual_call({"Created_By": {"id": RCC}}))
cr = ts.cancel_row(call("c9", desc="Wants Dr Smith.  \n", **MAN), "paid", "2026-10-19")
ck("the cancel says why under what the call said",
   cr == {"id": "c9", "Outgoing_Call_Status": "Cancelled",
          "Description": "Wants Dr Smith.\n\nClosed by the CRM cleanup on 2026-10-19: the patient had already paid."}, cr)
ck("... and stands alone on an empty call", ts.cancel_row(call("c9"), "stale", "2026-10-19")["Description"]
   == "Closed by the CRM cleanup on 2026-10-19: it was more than 90 days overdue.")
ck("the call queries read who made it and what it says", all(f in Q[p].split(" from ")[0] for p in ("deal_calls", "stale_calls")
                                                              for f in ("Created_By", "Description")))

ck("the deals a page needs looked up", ts.deal_ids([task(what="d-paid"), task(what="d-paid"), call(what="d-unsub"),
                                                    task(what="j1", mod="Journeys"), task()]) == ["d-paid", "d-unsub"])

# ---- the ledger ----------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "task_sweep.json")
    st = ts.Store(path)
    ck("shipped disarmed", st.data["armed"] is False and st.current() is None)
    st.start(C, "now")
    ck("a sweep runs the passes in order", st.current()[0] == "deal_tasks"
       and [p for p, _ in ts.PASSES] == ["deal_tasks", "deal_calls", "orphan_tasks", "stale_tasks", "stale_calls"])
    st.data["sweep"]["passes"]["deal_tasks"]["done"] = True
    ck("... and moves on when one is done", st.current()[:2] == ("deal_calls", "Calls"))
    st.spend("2026-10-19", 23)
    st.spend("2026-10-19", 3)
    ck("credits add up within a day", st.spent("2026-10-19") == 26)
    st.spend("2026-10-20", 5)
    ck("... and start over the next", st.spent("2026-10-20") == 5 and st.spent("2026-10-19") == 0)
    st.count("totals", "paid", 4)
    st.save()
    st2 = ts.Store(path)
    ck("the ledger survives a restart", st2.data["totals"] == {"paid": 4} and st2.current()[0] == "deal_calls")
    with open(path, "w") as f:
        f.write("{broken")
    ck("a broken file is reported, not fatal", "load_error" in ts.Store(path).data)

# ---- the app -------------------------------------------------------------------
import app as A  # noqa: E402

ck("the sweep is one of the worker loops", any(n == "loop:task-sweep" for n, _ in A._WORKER_LOOPS),
   [n for n, _ in A._WORKER_LOOPS])
ck("the ledger lives on the data disk", A._SWEEP_FILE.name == "task_sweep.json" and A._SWEEP_FILE.parent == A._PAID_FILE.parent)
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
ck("deletes need the arm AND the env switch",
   "armed = os.environ.get(\"TASK_SWEEP_WRITE\", \"1\") != \"0\" and bool(_sweep.data.get(\"armed\"))" in _src)
ck("the loop only steps while armed", "if _sweep.data.get(\"armed\") and os.environ.get(\"TASK_SWEEP_WRITE\", \"1\") != \"0\":"
   in _src)


class FakeZoho:
    """Rows bucketed by pass; a delete removes an id everywhere."""

    def __init__(self, buckets, team=None):
        self.buckets = buckets
        self.team = team if team is not None else [{"Team": "Coordinator", "Staff_User": {"id": "u-coord"}},
                                                   {"Team": "Biller", "Staff_User": {"id": "u-biller"}}]
        self.queries, self.deletes, self.cancels, self.throttle_delete_after = [], [], [], None

    def coql(self, q):
        self.queries.append(q)
        if "from Team_Schedules" in q:
            return self.team
        if "from Deals where Stage" in q:
            return [{"Scheduling_Owner": {"id": "u-briks"}}]
        if "from Deals where id in" in q:
            ids = re.search(r"in \(([^)]*)\)", q).group(1).split(",")
            return [DEALS[i] for i in ids if i in DEALS]
        p = next(p for p, _ in ts.PASSES if ts.query(p, C, "X").split(" where ")[1].split(" and id > ")[0]
                 == q.split(" where ")[1].split(" and id > ")[0])
        if q.startswith("select COUNT(id)"):
            return [{"COUNT(id)": len(self.buckets.get(p, []))}]
        cur = re.search(r"id > (\S+?)\)", q).group(1)
        lim = int(re.search(r"limit (\d+)$", q).group(1))
        rows = sorted((r for r in self.buckets.get(p, []) if cur == "0" or r["id"] > cur), key=lambda r: r["id"])
        return [dict(r) for r in rows[:lim]]

    def delete(self, module, ids):
        if self.throttle_delete_after is not None and len(self.deletes) >= self.throttle_delete_after:
            raise A._SweepThrottled("DELETE 429")
        self.deletes.append((module, list(ids)))
        for b in self.buckets.values():
            b[:] = [r for r in b if r["id"] not in ids]
        return set(ids)

    def cancel(self, module, rows):
        """A cancelled call is no longer Overdue: it leaves the pass."""
        self.cancels.append((module, [dict(r) for r in rows]))
        ids = {r["id"] for r in rows}
        for b in self.buckets.values():
            b[:] = [r for r in b if r["id"] not in ids]
        return ids


_saved = {k: getattr(A, k) for k in ("_sweep", "_sweep_coql", "_sweep_delete", "_sweep_cancel", "_sweep_today",
                                     "_v6_allowed")}
_saved_keep = dict(A._sweep_keep)
_page = ts.PAGE
_env = os.environ.pop("TASK_SWEEP_WRITE", None)
try:
    tdir = tempfile.mkdtemp()
    A._sweep_today = lambda: (TODAY, "-04:00")
    ts.PAGE = 3
    tasks = [task("t%02d" % n, what=w) for n, w in enumerate(("d-paid", "d-live", "d-unsub", "d-paid", "j1"), 1)]
    tasks[4]["$se_module"] = "Journeys"
    fz = FakeZoho({"deal_tasks": tasks, "deal_calls": [call("c01"), call("c02", desc="Prefers mornings", **MAN)],
                   "orphan_tasks": [task("o01", due="2026-10-01")],
                   "stale_tasks": [task("s01", what="d-live", due="2026-01-02"), task("s02", owner="u-briks", due="2026-01-02")],
                   "stale_calls": []})
    A._sweep_coql, A._sweep_delete, A._sweep_cancel = fz.coql, fz.delete, fz.cancel
    A._sweep_keep["owners"] = None
    A._sweep = ts.Store(os.path.join(tdir, "task_sweep.json"))

    info = A._sweep_step(label="t")
    ck("disarmed: reads and decides, deletes nothing", info.get("write") is False and info.get("to_delete") == 2
       and not fz.deletes, (info, fz.deletes))
    ck("... and the cursor does not move", A._sweep.current()[2]["cursor"] == "0", A._sweep.current())
    ck("the protected owners come from Team Schedules and the scheduling owners",
       any("from Team_Schedules" in q for q in fz.queries) and any("from Deals where Stage" in q for q in fz.queries))
    n = len([q for q in fz.queries if "from Team_Schedules" in q])
    A._sweep_step(label="t")
    ck("... read once an hour, not every step", len([q for q in fz.queries if "from Team_Schedules" in q]) == n)

    A._sweep.data["armed"] = True
    info = A._sweep_step(label="t")
    ck("armed: the first page's paid and dead go", fz.deletes == [("Tasks", ["t01", "t03"])] and info["deleted"] == 2,
       (info, fz.deletes))
    ck("... the live deal stays", any(r["id"] == "t02" for r in fz.buckets["deal_tasks"]))
    ck("... the cursor moves to the page's last id", A._sweep.current()[2]["cursor"] == "t03", A._sweep.current())
    ck("... counted by kind", A._sweep.data["totals"] == {"paid": 1, "dead": 1}, A._sweep.data["totals"])
    ck("... and the credits it cost: the page, one deal lookup, one delete (owners read an hour ago)",
       info["credits"] == 3, info)
    info = A._sweep_step(label="t")
    ck("the short page finishes the pass, the Journey's task kept", fz.deletes[-1] == ("Tasks", ["t04"])
       and A._sweep.data["sweep"]["passes"]["deal_tasks"]["done"] and info["kept"] == {"on a journey": 1}, info)
    info = A._sweep_step(label="t")
    ck("the calls pass: the automatic call deleted, the hand-booked one cancelled",
       fz.deletes[-1] == ("Calls", ["c01"]) and [r["id"] for r in fz.cancels[0][1]] == ["c02"]
       and info["deleted"] == 1 and info["cancelled"] == 1 and info["credits"] == 4, (info, fz.cancels))
    ck("... cancelled with its note under what it said",
       fz.cancels[0] == ("Calls", [{"id": "c02", "Outgoing_Call_Status": "Cancelled", "Description":
                                    "Prefers mornings\n\nClosed by the CRM cleanup on 2026-10-19: the patient had already paid."}]),
       fz.cancels)
    ck("... counted apart from the deletes", A._sweep.data["cancelled"] == {"paid": 1}
       and A._sweep.data["totals"] == {"paid": 3, "dead": 1}
       and A._sweep.data["sweep"]["passes"]["deal_calls"]["cancelled"] == 1, (A._sweep.data["cancelled"], A._sweep.data["totals"]))
    for _ in range(3):
        A._sweep_step(label="t")
    ck("every pass runs in turn", [d[1] for d in fz.deletes] == [["t01", "t03"], ["t04"], ["c01"], ["o01"], ["s01"]],
       fz.deletes)
    ck("Yevgen's stale task stays (scheduling owner)", [r["id"] for r in fz.buckets["stale_tasks"]] == ["s02"])
    ck("the sweep is marked finished", A._sweep.current() is None and A._sweep.data["sweep"]["finished"])
    n = len(fz.queries)
    ck("a finished sweep idles without reading", A._sweep_step(label="t").get("idle") and len(fz.queries) == n)
    A._sweep.data["sweep"]["finished"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    A._sweep_step(label="t")
    ck("... and starts over a day later", A._sweep.data["sweep"]["passes"]["deal_tasks"] ==
       {"cursor": "t05", "done": True, "read": 2, "deleted": 0, "cancelled": 0},
       A._sweep.data["sweep"]["passes"]["deal_tasks"])

    # The day's cap.
    A._sweep = ts.Store(os.path.join(tdir, "task_sweep2.json"))
    A._sweep.data["armed"] = True
    A._sweep.spend("2026-10-19", A._SWEEP_CAP - 10)
    n = len(fz.queries)
    ck("at the day's cap: waits, reads nothing", A._sweep_step(label="t").get("waiting") and len(fz.queries) == n)

    # A 429 halfway through the deletes.
    ts.PAGE = 250
    many = [task("m%03d" % k, what="d-paid") for k in range(250)]
    fz2 = FakeZoho({"deal_tasks": many})
    fz2.throttle_delete_after = 1
    A._sweep_coql, A._sweep_delete = fz2.coql, fz2.delete
    A._sweep_keep["owners"] = None
    A._sweep = ts.Store(os.path.join(tdir, "task_sweep3.json"))
    A._sweep.data["armed"] = True
    info = A._sweep_step(label="t")
    ck("100 ids a delete", len(fz2.deletes[0][1]) == 100, [len(d[1]) for d in fz2.deletes])
    ck("a 429 stops the step: what went is counted, the page is read again",
       info.get("throttled") and A._sweep.data["totals"] == {"paid": 100} and A._sweep.current()[2]["cursor"] == "0", info)
    fz2.throttle_delete_after = None
    A._sweep_step(label="t")
    ck("... and the rest go on the next step", len(fz2.buckets["deal_tasks"]) == 0 and A._sweep.data["totals"] == {"paid": 250},
       A._sweep.data["totals"])

    # No coordinators read: nothing at all.
    fz3 = FakeZoho({"deal_tasks": [task("x1", what="d-paid")]}, team=[])
    A._sweep_coql, A._sweep_delete = fz3.coql, fz3.delete
    A._sweep_keep["owners"] = None
    A._sweep = ts.Store(os.path.join(tdir, "task_sweep4.json"))
    A._sweep.data["armed"] = True
    info = A._sweep_step(label="t")
    ck("Team Schedules unreadable: an error, nothing deleted", "error" in info and not fz3.deletes, info)

    # The preview.
    ts.PAGE = 2000
    fz4 = FakeZoho({"deal_tasks": [task("p1", what="d-paid"), task("p2", what="d-live")], "stale_calls": [call("q1", what="d-live", start="2025-02-01T09:00:00-05:00")]})
    A._sweep_coql, A._sweep_delete = fz4.coql, fz4.delete
    A._sweep_keep["owners"] = None
    A._sweep = ts.Store(os.path.join(tdir, "task_sweep5.json"))
    pv = A._sweep_preview()
    ck("the preview decides and deletes nothing", not fz4.deletes and pv["passes"]["deal_tasks"]["sample_decisions"]
       == {"delete: paid": 1, "keep: live deal": 1} and pv["passes"]["stale_calls"]["open_in_range"] == 1, pv)
    ck("... reads 200 at a time", any(q.endswith("limit 200") for q in fz4.queries))

    # The HTTP edges.
    class R:
        def __init__(self, code, body=None):
            self.status_code, self._b = code, body
            self.ok, self.content, self.text = code < 400, b"x" if body is not None else b"", str(body)

        def json(self):
            return self._b
    A._sweep_coql, A._sweep_delete, A._sweep_cancel = _saved["_sweep_coql"], _saved["_sweep_delete"], _saved["_sweep_cancel"]
    _post, _delete, _put, _headers = A.requests.post, A.requests.delete, A.requests.put, A._zoho._headers
    seen = []
    try:
        A._zoho._headers = lambda: {}
        A.requests.delete = lambda url, **kw: seen.append((url, kw)) or R(200, {"data": [
            {"code": "SUCCESS", "details": {"id": "a"}}, {"code": "INVALID_DATA", "details": {"id": "b"}}]})
        got = A._sweep_delete("Calls", ["a", "b"])
        ck("a delete names the ids, fires no workflow, and reports what went",
           got == {"a"} and seen[0][0].endswith("/crm/v6/Calls") and seen[0][1]["params"] == {"ids": "a,b", "wf_trigger": "false"},
           (got, seen))
        A.requests.delete = lambda url, **kw: R(429, {})
        try:
            A._sweep_delete("Tasks", ["a"])
            ck("a delete 429 is a throttle", False)
        except A._SweepThrottled:
            ck("a delete 429 is a throttle", True)
        A.requests.post = lambda url, **kw: R(429, {})
        try:
            A._sweep_coql("select id from Tasks where id > 0")
            ck("a COQL 429 is a throttle", False)
        except A._SweepThrottled:
            ck("a COQL 429 is a throttle", True)
        A.requests.post = lambda url, **kw: R(204)
        ck("no rows is an empty page", A._sweep_coql("select id from Tasks where id > 0") == [])
        seen[:] = []
        A.requests.put = lambda url, **kw: seen.append((url, kw)) or R(200, {"data": [
            {"code": "SUCCESS", "details": {"id": "a"}}, {"code": "INVALID_DATA", "details": {"id": "b"}}]})
        rows = [{"id": "a", "Outgoing_Call_Status": "Cancelled"}, {"id": "b", "Outgoing_Call_Status": "Cancelled"}]
        got = A._sweep_cancel("Calls", rows)
        ck("a cancel is one update of the calls, no workflow fired, and reports what took",
           got == {"a"} and seen[0][0].endswith("/crm/v6/Calls") and seen[0][1]["json"] == {"data": rows, "trigger": []},
           (got, seen))
        A.requests.put = lambda url, **kw: R(429, {})
        try:
            A._sweep_cancel("Calls", rows)
            ck("a cancel 429 is a throttle", False)
        except A._SweepThrottled:
            ck("a cancel 429 is a throttle", True)
    finally:
        A.requests.post, A.requests.delete, A.requests.put, A._zoho._headers = _post, _delete, _put, _headers

    A._v6_allowed = lambda: False
    ck("the route is signed-in only", A.app.test_client().get("/api/v6/task-sweep").status_code == 401)
    A._v6_allowed = lambda: True
    A._sweep = ts.Store(os.path.join(tdir, "task_sweep6.json"))
    j = A.app.test_client().get("/api/v6/task-sweep?arm=1").get_json()
    ck("?arm=1 arms it", j["armed"] is True and A._sweep.data["armed"] is True, j)
    j = A.app.test_client().get("/api/v6/task-sweep?arm=0").get_json()
    ck("?arm=0 disarms it", j["armed"] is False and j["credits"]["cap"] == A._SWEEP_CAP and "cancelled" in j, j)
finally:
    for k, v in _saved.items():
        setattr(A, k, v)
    ts.PAGE = _page
    A._sweep_keep.update(_saved_keep)
    if _env is not None:
        os.environ["TASK_SWEEP_WRITE"] = _env

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
