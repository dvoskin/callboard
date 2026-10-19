"""The Books item catalog, kept equal to the estimator's quotes.

Danny, 2026-10-19: "make the Books item catalog match and ... automatically
sync the two". Run: ./.venv/bin/python -B checks_catalog_sync.py   (tally LAST)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog_sync as cs  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


# ---- the key: spacing and case fold, words do not ----
ck("case and spacing fold", cs.normalize("  Flexsculpt:  Chin/ Neck ") == "flexsculpt:chin/neck", cs.normalize("  Flexsculpt:  Chin/ Neck "))
ck("a trailing period folds", cs.normalize("Supply & Consumable Fee.") == "supply & consumable fee")
ck("'&' is not 'and'", cs.normalize("Chin & Jawline") != cs.normalize("Chin and Jawline"))
ck("a plural is its own key", cs.normalize("Massage") != cs.normalize("Massages"))


# ---- collect: headers skipped, latest wins, every rate kept ----
def _est(eid, created, lines, day=None):
    return {"estimate_id": eid, "estimate_number": "GPS-%s" % eid, "date": day or created[:10], "created_time": created,
            "line_items": [{"name": n, "rate": r, "quantity": 1, "item_id": i, "line_item_category": c, "description": d}
                           for (n, r, i, c, d) in lines]}

E1 = _est("1", "2026-10-16T10:00:00-0400", [("Procedure", 0, "", "header", "Procedure"),
                                           ("Liposuction: Chin & Jawline", 7250, "", "line_item", "old words"),
                                           ("Local Anesthesia (Included)", 0, "it-anes", "line_item", "numb"),
                                           ("Promo: CHIN PROMO", -3750, "", "line_item", "cash only"),
                                           ("   ", 5, "", "line_item", "blank name")])
E2 = _est("2", "2026-10-18T12:00:00-0400", [("Liposuction: Chin & Jawline ", 7250, "", "line_item", "new words"),
                                           ("Supply & Consumable Fee", 300, "", "line_item", "")])
E3 = _est("3", "2026-10-17T09:00:00-0400", [("Supply & Consumable Fee", 700, "", "line_item", "")])
T = cs.collect([E1, E2, E3])
ck("headers and blank names are not items", "procedure" not in T and "" not in T, sorted(T))
ck("four names collected", sorted(T) == ["liposuction:chin & jawline", "local anesthesia (included)", "promo:chin promo",
                                          "supply & consumable fee"], sorted(T))
chin = T["liposuction:chin & jawline"]
ck("with one quote each way the latest quote's words win, and the name is trimmed", chin["description"] == "new words" and chin["name"] == "Liposuction: Chin & Jawline"
   and chin["count"] == 2 and chin["last_seen"].startswith("2026-10-18") and chin["first_seen"].startswith("2026-10-16"), chin)
ck("one rate, no conflict, support = 2", chin["rates"] == {"7250": 2} and chin["conflict"] is False and chin["support"] == 2, chin)
sup = T["supply & consumable fee"]
ck("two rates in the window is a conflict; the latest (300, 10-18) is the rate, support 1",
   sup["conflict"] is True and sup["rate"] == 300 and sup["rates"] == {"700": 1, "300": 1} and sup["support"] == 1, sup)
# the estimator swaps neighbouring descriptions on some quotes: the words most quotes carry win
SW = [_est("s%d" % i, "2026-10-18T1%d:00:00-0400" % i, [("BBL Pillow", 200, "", "line_item", "pillow words")]) for i in range(3)]
SW.append(_est("s9", "2026-10-18T19:00:00-0400", [("BBL Pillow", 200, "", "line_item", "faja words, swapped")]))
ck("a description swapped on the LATEST quote does not win over the words three quotes carry",
   cs.collect(SW)["bbl pillow"]["description"] == "pillow words" and cs.collect(SW)["bbl pillow"]["description_variants"] == 2, cs.collect(SW)["bbl pillow"])
ck("a line that carried an item_id keeps it", T["local anesthesia (included)"]["item_id"] == "it-anes")
ck("a promo's negative rate is kept as is", T["promo:chin promo"]["rate"] == -3750)
ck("the compact stored shape (lines/category) collects the same", cs.collect([{"created_time": "x", "lines": [
    {"name": "A", "rate": 1, "category": "line_item", "description": ""}, {"name": "H", "rate": 0, "category": "header"}]}]).keys() == {"a"})


# ---- diff: the plan ----
ITEMS = [{"item_id": "i-anes", "name": "Local Anesthesia (Included)", "status": "active", "rate": 0, "description": "numb", "created_time": "2023"},
         {"item_id": "i-sup", "name": "Supply and Consumable Fee", "status": "active", "rate": 700, "description": "", "created_time": "2023"},
         {"item_id": "i-sup2", "name": "Supply & Consumable Fee", "status": "active", "rate": 700, "description": "", "created_time": "2023"},
         {"item_id": "i-flex", "name": "Flexsculpt: Chin/ Neck", "status": "active", "rate": 2500, "description": "", "created_time": "2023"},
         {"item_id": "i-promo-old", "name": "Promo: CHIN PROMO", "status": "inactive", "rate": -3750, "description": "cash only", "created_time": "2024"},
         {"item_id": "i-promo-new", "name": "promo: chin promo", "status": "active", "rate": -3000, "description": "cash only", "created_time": "2025"},
         {"item_id": "i-dead", "name": "Old thing", "status": "inactive", "rate": 1, "description": "", "created_time": "2022"}]
P = cs.diff(T, ITEMS)
names = lambda k, plan=None: sorted(e["name"] for e in (plan or P)[k])  # noqa: E731
ck("the chin procedure does not exist in Books -> create", names("create") == ["Liposuction: Chin & Jawline"], names("create"))
ck("...flagged as a positive rate", P["create"][0]["negative"] is False)
ck("local anesthesia already equal -> unchanged", names("unchanged") == ["Local Anesthesia (Included)"], names("unchanged"))
ck("rates moved on ONE quote each (supply 700->300, promo -3000->-3750) -> held, not written",
   names("conflict") == ["Promo: CHIN PROMO", "Supply & Consumable Fee"] and P["update"] == [], (names("conflict"), P["update"]))
ck("...and each held line says why", {e["name"]: e["reason"] for e in P["conflict"]} == {"Promo: CHIN PROMO": "new rate seen on 1 quote(s)",
   "Supply & Consumable Fee": "several rates in the window"}, [e.get("reason") for e in P["conflict"]])
ck("a duplicate key in Books prefers the active item and reports the pair",
   [e["item_id"] for e in P["conflict"] if e["key"] == "promo:chin promo"] == ["i-promo-new"]
   and [d["key"] for d in P["duplicates"]] == ["promo:chin promo"], P["duplicates"])
ck("stale = active items the estimator did not quote, 'Supply and Consumable Fee' and the Flexsculpt row",
   sorted(s_["name"] for s_ in P["stale"]) == ["Flexsculpt: Chin/ Neck", "Supply and Consumable Fee"], P["stale"])
ck("an inactive unquoted item is not stale", not any(s_["name"] == "Old thing" for s_ in P["stale"]))
ck("summary counts every bucket", P["summary"] == {"create": 1, "update": 0, "reactivate": 0, "unchanged": 1, "conflict": 2, "stale": 2,
                                                   "duplicates": 1, "manual": 0}, P["summary"])
# the new rate on a second quote clears the hold
E4 = _est("4", "2026-10-18T13:00:00-0400", [("Supply & Consumable Fee", 300, "", "line_item", "")])
P2 = cs.diff(cs.collect([E1, E2, E3, E4]), ITEMS)
ck("the supply fee at 300 on two quotes becomes an update", [e["changes"] for e in P2["update"] if e["name"] == "Supply & Consumable Fee"] == [["rate"]]
   and "Supply & Consumable Fee" not in names("conflict", P2), (P2["update"], P2["conflict"]))
# one quote at an odd price on a positive item: held; two quotes: written
CHIN_ITEM = [{"item_id": "i-chin", "name": "liposuction: chin & jawline", "status": "active", "rate": 7000, "description": "new words"}]
P7 = cs.diff(cs.collect([E2]), CHIN_ITEM)
ck("one quote at a new price does not move the catalog", names("conflict", P7) == ["Liposuction: Chin & Jawline"] and P7["update"] == [], P7["summary"])
P8 = cs.diff(cs.collect([E1, E2]), CHIN_ITEM)
ck("two quotes at the new price do", [(e["item_id"], e["changes"]) for e in P8["update"]] == [("i-chin", ["rate"])], P8["update"])
ck("a description-only change needs no second quote", [e["changes"] for e in cs.diff(cs.collect([E2]), [dict(CHIN_ITEM[0], rate=7250, description="old")])["update"]] == [["description"]])
# negatives: never through the API
P9 = cs.diff(cs.collect([E1]), [])
ck("a promo new to Books is MANUAL, never a create (Books API code 2012)", names("manual", P9) == ["Promo: CHIN PROMO"] and "Promo: CHIN PROMO" not in names("create", P9)
   and "2012" in P9["manual"][0]["reason"], (P9["manual"], names("create", P9)))
E1b = _est("1b", "2026-10-16T11:00:00-0400", [("Promo: CHIN PROMO", -3750, "", "line_item", "cash only")])
P10 = cs.diff(cs.collect([E1, E1b]), [ITEMS[5]])
ck("a promo whose price moved with support is MANUAL too, with the change named", [(e["item_id"], e["changes"]) for e in P10["manual"]] == [("i-promo-new", ["rate"])]
   and P10["update"] == [], P10["summary"])
P3 = cs.diff(cs.collect([E1]), [ITEMS[4]])
ck("an inactive item still quoted, already equal -> reactivate only", [e["item_id"] for e in P3["reactivate"]] == ["i-promo-old"] and P3["update"] == [] and P3["unchanged"] == [], P3["summary"])
P4 = cs.diff(cs.collect([E1]), [dict(ITEMS[4], name="Liposuction: Chin & Jawline", rate=7250, description="x")])
ck("...and reactivate + update when its words moved", P4["summary"]["reactivate"] == 1 and P4["summary"]["update"] == 1, P4["summary"])
# a line quoted at two rates that is NOT in Books yet: created at the rate seen most often, flagged
E5 = _est("5", "2026-10-18T14:00:00-0400", [("Leg Stockings", 300, "", "line_item", "")])
E6 = _est("6", "2026-10-18T15:00:00-0400", [("Leg Stockings", 0, "", "line_item", "")])
E7 = _est("7", "2026-10-18T16:00:00-0400", [("Leg Stockings", 300, "", "line_item", "")])
E8 = _est("8", "2026-10-18T17:00:00-0400", [("Leg Stockings", 0, "", "line_item", "")])
E9 = _est("9", "2026-10-18T18:00:00-0400", [("Leg Stockings", 300, "", "line_item", "")])
E10 = _est("10", "2026-10-18T19:00:00-0400", [("Leg Stockings", 0, "", "line_item", "")])
P5 = cs.diff(cs.collect([E5, E6, E7, E8, E9]), [])
ck("a conflicting line new to Books is created at the rate seen most often, not the latest",
   P5["create"][0]["rate"] == 300 and P5["create"][0]["conflict"] is True and P5["create"][0]["rates"] == {"300": 3, "0": 2}, P5["create"])
P6 = cs.diff(cs.collect([E5, E6, E7, E8, E9, E10]), [])
ck("...and a tie goes to the higher rate", P6["create"][0]["rate"] == 300, P6["create"])


# ---- apply: every outcome kept, a failure isolates ----
class _Client:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)
    def create_item(self, body):
        self.calls.append(("create", body))
        if body["name"] in self.fail:
            raise RuntimeError("Books create item HTTP 400: refused")
        return {"item_id": "new-" + body["name"][:4]}
    def update_item(self, item_id, body):
        self.calls.append(("update", item_id, body))
        return {"item_id": item_id}
    def activate_item(self, item_id):
        self.calls.append(("activate", item_id))
        return {"item_id": item_id}

ITEMS_A = [dict(ITEMS[0], description="old numb"), CHIN_ITEM[0]]
PA = cs.diff(cs.collect([E1, E2]), ITEMS_A)
C = _Client()
R = cs.apply(PA, C, pause=0)
ck("the plan is one create (supply fee), two updates (anesthesia words, chin rate) and the promo left manual",
   [c[0] for c in C.calls] == ["create", "update", "update"] and names("manual", PA) == ["Promo: CHIN PROMO"], (C.calls, PA["summary"]))
ck("the create is a sales service at the quoted rate", C.calls[0][1] == {"name": "Supply & Consumable Fee", "rate": 300, "description": "",
   "item_type": "sales", "product_type": "service"}, C.calls[0])
ck("an update keeps the Books spelling of the name and sends the rate and words",
   ("update", "i-chin", {"name": "liposuction: chin & jawline", "rate": 7250, "description": "new words"}) in C.calls, C.calls)
ck("nothing negative, held or stale is ever sent", not any("Promo" in str(c) or "Flexsculpt" in str(c) or c[1] == "i-sup" for c in C.calls), C.calls)
ck("results carry the new item id", R["created"][0]["item_id"] == "new-Supp" and R["errors"] == [], R)
C2 = _Client(fail=("Supply & Consumable Fee",))
R2 = cs.apply(PA, C2, pause=0)
ck("a refused create is an error on the result and the updates still happen", len(R2["errors"]) == 1 and "400" in R2["errors"][0]["error"] and len(R2["updated"]) == 2, R2)
long_plan = {"create": [{"key": "x", "name": "X", "rate": 1, "description": "d" * 3000}], "update": [], "reactivate": []}
C3 = _Client(); cs.apply(long_plan, C3, pause=0)
ck("descriptions are cut to Books' 2000", len(C3.calls[0][1]["description"]) == 2000)


# ---- the store: round trip, the window, forgetting ----
tmp = os.path.join(tempfile.mkdtemp(), "catalog_sync.json")
S = cs.Store(tmp)
S.remember(E1); S.remember(E2); S.remember(E3)
S.data["armed"] = True
S.save()
S2 = cs.Store(tmp)
ck("what was remembered survives a reload, with the armed flag", set(S2.data["seen"]) == {"1", "2", "3"} and S2.data["armed"] is True
   and S2.data["estimates"]["1"]["lines"][1]["name"] == "Liposuction: Chin & Jawline", S2.data["seen"])
ck("the stored shape keeps header/line_item so collect can skip headers", S2.data["estimates"]["1"]["lines"][0]["category"] == "header")
ck("the window is by estimate date", [e["number"] for e in S2.window("2026-10-17")] == ["GPS-2", "GPS-3"] or sorted(e["number"] for e in S2.window("2026-10-17")) == ["GPS-2", "GPS-3"], S2.window("2026-10-17"))
S2.forget_before("2026-10-17")
ck("forgetting drops the id and the quote together", "1" not in S2.data["seen"] and "1" not in S2.data["estimates"] and "2" in S2.data["seen"])
S3 = cs.Store(os.path.join(tempfile.mkdtemp(), "none.json"))
ck("a missing file is an empty store, not an error", S3.data["seen"] == {} and "load_error" not in S3.data)


# ---- run_once: window filtered locally, budget capped, disarmed plans only, armed writes ----
class _Books(_Client):
    def __init__(self, heads, details, items, fail=()):
        super().__init__(fail)
        self.heads, self.details, self.items, self.windows, self.read = heads, details, items, [], []
    def list_estimates_window(self, a, b):
        self.windows.append((a, b)); return self.heads
    def get_estimate(self, i):
        self.read.append(i)
        if i == "bad":
            raise RuntimeError("HTTP 500")
        return self.details[i]
    def list_items_all(self):
        return self.items

HEADS = [{"estimate_id": "1", "date": "2026-10-16"}, {"estimate_id": "2", "date": "2026-10-18"}, {"estimate_id": "bad", "date": "2026-10-19"},
         {"estimate_id": "old", "date": "2026-09-01"}, {"estimate_id": "nodate"}]
tmp2 = os.path.join(tempfile.mkdtemp(), "c.json")
S4 = cs.Store(tmp2)
B = _Books(HEADS, {"1": E1, "2": E2}, ITEMS)
info = cs.run_once(B, S4, days=3, armed=False, today=date(2026, 10, 19), pause=0)
ck("the window reaches back `days` and three days forward (the evening-quote date trap)", B.windows == [("2026-10-16", "2026-10-22")], B.windows)
ck("a quote outside the window -- or with no date -- is never read, even if the API ignored the filter",
   "old" not in B.read and "nodate" not in B.read and info["listed"] == 3, (B.read, info))
ck("two quotes read, the broken one recorded, nothing written while disarmed",
   info["fetched"] == 2 and info["failed"][0]["estimate_id"] == "bad" and info["applied"] is None and B.calls == [], info)
ck("the plan is on disk for /api/v6/catalog-sync, manual bucket included",
   json.load(open(tmp2))["last_plan"]["summary"]["create"] == 1 and "manual" in json.load(open(tmp2))["last_plan"], json.load(open(tmp2))["last_plan"]["summary"])
info2 = cs.run_once(B, S4, days=3, armed=False, today=date(2026, 10, 19), pause=0)
ck("a second run re-reads only what it has not seen (the broken one)", info2["new"] == 1 and info2["fetched"] == 0, info2)
info3 = cs.run_once(B, S4, days=3, armed=True, today=date(2026, 10, 19), pause=0)
ck("armed: the chin create is written; the one-quote rate moves are held", info3["applied"]["created"] == 1 and info3["applied"]["updated"] == 0
   and [c[0] for c in B.calls] == ["create"], (info3["applied"], B.calls))
S5 = cs.Store(os.path.join(tempfile.mkdtemp(), "e.json"))
B5 = _Books(HEADS[:2], {"1": E1, "2": E2}, ITEMS)
info5 = cs.run_once(B5, S5, days=3, armed=True, today=date(2026, 10, 19), pause=0, max_fetch=1)
ck("over the per-run read cap: one read, one deferred, and NOTHING written from half a window",
   B5.read == ["1"] and info5["deferred"] == 1 and info5["applied"] is None and info5.get("held_for_deferred") and B5.calls == [], (B5.read, info5))
info6 = cs.run_once(B5, S5, days=3, armed=True, today=date(2026, 10, 19), pause=0, max_fetch=1)
ck("...the next run reads the rest and writes", B5.read == ["1", "2"] and info6["deferred"] == 0 and info6["applied"]["created"] == 1, (B5.read, info6))
class _Dead(_Books):
    def list_estimates_window(self, a, b):
        raise RuntimeError("Books API auth error 401 -- lacks settings scope")
info4 = cs.run_once(_Dead([], {}, []), cs.Store(os.path.join(tempfile.mkdtemp(), "d.json")), days=3, armed=True, today=date(2026, 10, 19), pause=0)
ck("a Books refusal is an error on the run, not an exception", "401" in info4["error"] and "took" in info4, info4)


# ---- the Books client: newest first, paging stops at the window, items under settings scope ----
import books_client as bc  # noqa: E402
_seen = {}
class _BC(bc.BooksClient):
    def __init__(self):
        pass
    def _list_documents(self, doc_type, a, b, n, **kw):
        _seen.update(kw, doc_type=doc_type, a=a, b=b, n=n); return []
_BC().list_estimates_window("2026-10-16", "2026-10-22")
ck("the estimate list is newest first and stops before the window", _seen.get("sort_column") == "created_time" and _seen.get("sort_order") == "D"
   and _seen.get("stop_before") == "2026-10-16" and _seen.get("n") <= 2000, _seen)


# ---- the app: the loop is registered, the route gates, the file is on the data disk ----
import app as A  # noqa: E402
ck("the hourly loop is one of the worker loops", any(n == "loop:catalog" for n, _ in A._WORKER_LOOPS), [n for n, _ in A._WORKER_LOOPS])
ck("the state file sits on the data disk next to the opt-out list", A._CATALOG_FILE.name == "catalog_sync.json" and A._CATALOG_FILE.parent == A._OPTOUT_FILE.parent, str(A._CATALOG_FILE))
_c = A.app.test_client()
_real = A._v6_allowed
A._v6_allowed = lambda: False
try:
    r1 = _c.get("/api/v6/catalog-sync?arm=1&kick=1")
finally:
    A._v6_allowed = _real
ck("signed out: refused, and neither armed nor kicked", r1.status_code == 401 and not A._catalog.data.get("armed") and A._catalog_state.get("running") is False, (r1.status_code, A._catalog.data.get("armed")))
A._v6_allowed = lambda: True
_real_run = A._catalog_run
A._catalog_run = lambda days=3: None
try:
    r2 = _c.get("/api/v6/catalog-sync?table=1")
    r3 = _c.get("/api/v6/catalog-sync?arm=0")
finally:
    A._v6_allowed = _real
    A._catalog_run = _real_run
ck("signed in: the plan, the runs and the table are served", r2.status_code == 200 and set(r2.get_json()) >= {"armed", "plan", "runs", "table", "quotes_remembered"}, r2.get_json() and sorted(r2.get_json()))
ck("ships DISARMED", A._catalog.data.get("armed") is False and r3.get_json()["armed"] is False, A._catalog.data.get("armed"))

for e in errors:
    print("  FAIL", e)
print("%d passed" % passed)
print("\n%d failed" % len(errors))
sys.exit(1 if errors else 0)
