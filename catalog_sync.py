"""The Books item catalog, kept equal to what the estimator quotes.

Danny, 2026-10-19: "make the Books item catalog match and ... add a feature
that automatically syncs the two".

The estimator (estimator.goalsplasticsurgery.com, a PHP app with no source
here) writes every quote line into Zoho Books as FREE TEXT: name, rate and
description, no item_id. So the Items list in Books -- the thing finance,
reports and the CRM Products module read -- never learns a price change.
On 2026-10-19 "Liposuction: Chin & Jawline" was quoted at $7,250 on every
quote and did not exist as an item; the nearest items were two Flexsculpt
rows at $2,500 and $5,000 from 2023-24.

The estimator's price table is not readable anywhere except in the quotes it
has already written, so the quotes ARE the source of truth here:

  collect(estimates)      every line_item (headers skipped) folded by name:
                          the latest rate and description, how many quotes
                          carried it, and every distinct rate seen
  diff(wanted, items)     the plan -- create / update / reactivate /
                          unchanged -- plus two lists that are reported and
                          never acted on: conflicts (one name, several rates
                          in the window) and stale (active items the
                          estimator has not quoted in the window)
  apply(plan, client)     the writes, one item at a time, each result kept;
                          nothing is ever deleted or deactivated
  Store                   /data/catalog_sync.json -- the ids already read, the
                          current table, the last runs, and the ARMED flag

Shipped DISARMED: every run builds the plan and writes nothing until
/api/v6/catalog-sync?arm=1, after the first plan has been read. A plan is the
visible proof that the matching is right BEFORE it touches the catalog.

Conflicts hold, they do not flap. A name quoted at two rates inside the
window gets no update unless its latest rate has been seen at least
MIN_SUPPORT times; a create always takes the latest. Promo lines carry a
negative rate; Books may or may not accept that as an item, and the answer
is recorded on the plan item rather than guessed here.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

MIN_SUPPORT = 2
_WS = re.compile(r"\s+")


def normalize(name) -> str:
    """The matching key: case, spacing and the spaces people type around a
    slash or colon do not make a different item. "Flexsculpt: Chin/ Neck" and
    "flexsculpt:chin/neck" are one key. Nothing else is folded -- "&" is not
    "and", a plural is not its singular -- because a wrong merge silently
    rewrites the price of a different service."""
    s = _WS.sub(" ", str(name or "")).strip().lower()
    s = re.sub(r"\s*([/:])\s*", r"\1", s)
    return s.rstrip(".")


def _line_ok(li) -> bool:
    if (li.get("line_item_category") or li.get("category") or "line_item") == "header":
        return False
    return bool(str(li.get("name") or "").strip())


def collect(estimates) -> dict:
    """Fold quote lines into the table: {key: {...}}.

    estimates: iterable of Books estimate dicts (the /estimates/{id} shape,
    or the compact shape this module stores). Latest wins by created_time,
    then by estimate date, so a price change shows up on the next run and the
    old rate stays visible in `rates` until it leaves the window."""
    table: dict = {}
    for e in estimates:
        when = e.get("created_time") or e.get("date") or ""
        for li in e.get("line_items") or e.get("lines") or []:
            if not _line_ok(li):
                continue
            name = _WS.sub(" ", str(li["name"])).strip()
            key = normalize(name)
            try:
                rate = float(li.get("rate") or 0)
            except (TypeError, ValueError):
                continue
            desc = str(li.get("description") or "").strip()
            rec = table.get(key)
            if rec is None:
                rec = table[key] = {"name": name, "rate": rate, "description": desc, "count": 0,
                                    "rates": {}, "first_seen": when, "last_seen": when,
                                    "item_id": li.get("item_id") or "", "support": 0}
            rec["count"] += 1
            rk = _money(rate)
            rec["rates"][rk] = rec["rates"].get(rk, 0) + 1
            # The estimator sometimes swaps the descriptions of neighbouring
            # Extras lines (a BBL Pillow carrying the faja text, seen twice on
            # 2026-10-16/17). The words that go on the item are the ones MOST
            # quotes carry, not the last quote's -- a swap on one quote cannot
            # rewrite the catalog.
            dc = rec.setdefault("_descs", {})
            dc[desc] = dc.get(desc, 0) + 1
            if li.get("item_id") and not rec["item_id"]:
                rec["item_id"] = li["item_id"]
            if when < (rec["first_seen"] or when):
                rec["first_seen"] = when
            if when >= (rec["last_seen"] or ""):
                rec["last_seen"], rec["name"], rec["rate"], rec["description"] = when, name, rate, desc
    for rec in table.values():
        rec["support"] = rec["rates"].get(_money(rec["rate"]), 0)
        rec["conflict"] = len(rec["rates"]) > 1
        dc = rec.pop("_descs", {})
        if dc:
            latest = rec["description"]
            best = max(dc.items(), key=lambda kv: (kv[1], kv[0] == latest, len(kv[0])))[0]
            rec["description"] = best
            rec["description_variants"] = len(dc)
    return table


def _money(x) -> str:
    return ("%.2f" % float(x)).rstrip("0").rstrip(".")


def diff(wanted: dict, items: list, min_support: int = MIN_SUPPORT) -> dict:
    """The plan. `items` is the Books item list (any status).

    create      no item of that name -> a new item at the latest rate
    update      an item whose rate or description differs from the latest
    reactivate  an inactive item the estimator is still quoting
    unchanged   already equal
    conflict    the rate moved but the new rate is on fewer than
                min_support quotes: reported, held, NOT written
    manual      a negative price (promos): Books' API refuses it, so it is
                reported for the web app and never attempted
    stale       active items never quoted in the window: reported only
    """
    by_key: dict = {}
    for it in items:
        k = normalize(it.get("name"))
        # Two Books items with the same key: keep the active one, else the
        # newest, and say so -- a duplicate is a finding, not a tie-break.
        cur = by_key.get(k)
        if cur is None or (it.get("status") == "active" and cur.get("status") != "active") or (
                it.get("status") == cur.get("status") and (it.get("created_time") or "") > (cur.get("created_time") or "")):
            by_key[k] = it
    plan = {"create": [], "update": [], "reactivate": [], "unchanged": [], "conflict": [], "stale": [],
            "duplicates": [], "manual": []}
    seen_names: dict = {}
    for it in items:
        seen_names.setdefault(normalize(it.get("name")), []).append(it)
    for k, lst in seen_names.items():
        if len(lst) > 1:
            plan["duplicates"].append({"key": k, "items": [{"item_id": x.get("item_id"), "name": x.get("name"),
                                                            "status": x.get("status"), "rate": x.get("rate")} for x in lst]})
    for k, w in sorted(wanted.items()):
        it = by_key.get(k)
        entry = {"key": k, "name": w["name"], "rate": w["rate"], "description": w["description"],
                 "count": w["count"], "last_seen": w["last_seen"], "rates": w["rates"], "negative": w["rate"] < 0}
        if it is None:
            if w.get("conflict"):
                # A line quoted at several rates (the supply fee is $300 cash
                # and $0 inside a package) has no single "latest" worth
                # creating from: the create takes the rate seen MOST often and
                # says so; later runs hold it as a conflict until one rate wins.
                mode = max(w["rates"].items(), key=lambda kv: (kv[1], kv[0]))[0]
                entry["rate"] = float(mode)
                entry["negative"] = entry["rate"] < 0
                entry["conflict"] = True
            if entry["negative"]:
                # Books refuses a negative item price through the API (code
                # 2012, "The price cannot be negative", 2026-10-19, with the
                # Sales and the Discount account alike). Promos live in Books
                # at negative rates only because someone typed them in the
                # web app. Reported every run, never attempted.
                entry["reason"] = "negative price: Books API refuses it (code 2012); create it in the Books web app"
                plan["manual"].append(entry)
                continue
            plan["create"].append(entry)
            continue
        entry["item_id"] = it.get("item_id")
        entry["books_name"] = it.get("name")
        entry["books_rate"] = it.get("rate")
        entry["books_status"] = it.get("status")
        rate_same = abs(float(it.get("rate") or 0) - float(w["rate"])) < 0.005
        desc_same = (str(it.get("description") or "").strip() == w["description"])
        # A catalog price moves only on evidence: the new rate on at least
        # min_support quotes in the window. One quote at an odd rate -- a
        # rep's hand edit, a one-off -- is reported, never written. A real
        # price change is quoted dozens of times a day and clears this in
        # the next hour.
        if not rate_same and w.get("support", 0) < min_support:
            entry["reason"] = ("several rates in the window" if w.get("conflict") else "new rate seen on %d quote(s)"
                               % w.get("support", 0))
            plan["conflict"].append(entry)
            continue
        if (w["rate"] < 0 or float(it.get("rate") or 0) < 0) and not (rate_same and desc_same):
            entry["reason"] = "negative price: Books API refuses it (code 2012); edit it in the Books web app"
            entry["changes"] = ([] if rate_same else ["rate"]) + ([] if desc_same else ["description"])
            plan["manual"].append(entry)
            continue
        if it.get("status") == "inactive":
            plan["reactivate"].append(entry)
        if rate_same and desc_same:
            if it.get("status") != "inactive":
                plan["unchanged"].append(entry)
            continue
        entry["changes"] = ([] if rate_same else ["rate"]) + ([] if desc_same else ["description"])
        plan["update"].append(entry)
    for k, it in by_key.items():
        if k not in wanted and it.get("status") == "active":
            plan["stale"].append({"key": k, "item_id": it.get("item_id"), "name": it.get("name"),
                                  "rate": it.get("rate"), "last_modified_time": it.get("last_modified_time")})
    plan["summary"] = {n: len(plan[n]) for n in ("create", "update", "reactivate", "unchanged", "conflict",
                                                 "stale", "duplicates", "manual")}
    return plan


def apply(plan: dict, client, log=None, pause: float = 0.4) -> dict:
    """Write the plan through `client` (create_item / update_item /
    activate_item). Every item's outcome is kept on the result; one failure
    never stops the rest. Returns {created, updated, reactivated, errors}."""
    out = {"created": [], "updated": [], "reactivated": [], "errors": []}

    def _try(kind, entry, fn):
        try:
            r = fn()
            rec = {"key": entry["key"], "name": entry["name"], "rate": entry["rate"],
                   "item_id": (r or {}).get("item_id") or entry.get("item_id")}
            out[kind].append(rec)
            return rec
        except Exception as e:  # noqa: BLE001
            msg = "%s: %s" % (type(e).__name__, str(e)[:300])
            out["errors"].append({"kind": kind, "key": entry["key"], "name": entry["name"],
                                  "rate": entry["rate"], "error": msg})
            if log:
                log.warning("catalog %s failed for %r: %s", kind, entry["name"], msg)
        finally:
            time.sleep(pause)

    for entry in plan.get("reactivate", []):
        _try("reactivated", entry, lambda: client.activate_item(entry["item_id"]))
    for entry in plan.get("create", []):
        body = {"name": entry["name"], "rate": entry["rate"], "description": entry["description"][:2000],
                "item_type": "sales", "product_type": "service"}
        _try("created", entry, lambda: client.create_item(body))
    for entry in plan.get("update", []):
        body = {"name": entry.get("books_name") or entry["name"], "rate": entry["rate"],
                "description": entry["description"][:2000]}
        _try("updated", entry, lambda: client.update_item(entry["item_id"], body))
    return out


class Store:
    """{seen: {estimate_id: date}, table: {...}, runs: [...], armed: bool}"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = {"seen": {}, "table": {}, "runs": [], "armed": False, "estimates": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("seen"), dict):
                self.data.update({k: d.get(k, v) for k, v in self.data.items()})
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            self.data["load_error"] = "%s: %s" % (type(e).__name__, e)
        return self.data

    def save(self):
        tmp = str(self.path) + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.replace(tmp, str(self.path))

    def remember(self, estimate: dict):
        """Keep the compact shape of one quote so the table can be rebuilt
        from the window without re-reading Books."""
        eid = str(estimate.get("estimate_id"))
        lines = [{"name": li.get("name"), "rate": li.get("rate"), "item_id": li.get("item_id") or "",
                  "category": li.get("line_item_category") or li.get("category") or "line_item",
                  "description": li.get("description") or ""}
                 for li in (estimate.get("line_items") or estimate.get("lines") or [])]
        with self._lock:
            self.data["seen"][eid] = estimate.get("date") or ""
            self.data["estimates"][eid] = {"date": estimate.get("date"), "created_time": estimate.get("created_time"),
                                           "number": estimate.get("estimate_number"), "lines": lines}

    def forget_before(self, day: str):
        with self._lock:
            for eid in [k for k, d in self.data["seen"].items() if (d or "") < day]:
                self.data["seen"].pop(eid, None)
                self.data["estimates"].pop(eid, None)

    def window(self, day_from: str):
        return [e for e in self.data["estimates"].values() if (e.get("date") or "") >= day_from]

    def note_run(self, info):
        with self._lock:
            self.data["runs"] = (self.data["runs"] + [info])[-20:]


def run_once(books, store: Store, days: int, armed: bool, log=None, today=None, pause: float = 0.65,
             max_fetch: int = 300) -> dict:
    """One pass: list the window, read the quotes not yet seen, rebuild the
    table, plan, and -- only when armed -- apply. Never raises."""
    t0 = time.time()
    today = today or datetime.now(timezone.utc).date()
    from datetime import timedelta
    # Books filters on the estimate DATE and the estimator stamps a quote
    # written in the evening with the next day's date; three days of slack
    # on the near side, and the whole window on the far side.
    day_from = (today - timedelta(days=max(1, days))).isoformat()
    day_to = (today + timedelta(days=3)).isoformat()
    info = {"started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": days,
            "armed": bool(armed), "window": [day_from, day_to]}
    try:
        heads = books.list_estimates_window(day_from, day_to)
        # The date filter is not to be trusted: listing through the Zoho MCP
        # on 2026-10-19 returned every estimate with date_start/date_end set.
        # Keep only the window here, so a filter the API ignores costs list
        # pages, never thousands of detail reads.
        heads = [h for h in heads if day_from <= (h.get("date") or "") <= day_to]
        ids = [str(h.get("estimate_id")) for h in heads if h.get("estimate_id")]
        new = [i for i in ids if i not in store.data["seen"]]
        # Every call counts against the org's daily Books quota, which the
        # estimator itself spends. A normal hour is a handful of new quotes;
        # the cap only bites on a first run or a long outage, and the rest
        # are read on the next pass.
        deferred = max(0, len(new) - max_fetch)
        new = new[:max_fetch]
        info.update({"listed": len(ids), "new": len(new), "deferred": deferred})
        fetched, failed = 0, []
        for i in new:
            try:
                store.remember(books.get_estimate(i))
                fetched += 1
            except Exception as e:  # noqa: BLE001
                failed.append({"estimate_id": i, "error": "%s: %s" % (type(e).__name__, str(e)[:160])})
            time.sleep(pause)
        info.update({"fetched": fetched, "failed": failed[:20]})
        store.forget_before((today - timedelta(days=90)).isoformat())
        table = collect(store.window(day_from))
        store.data["table"] = table
        items = books.list_items_all()
        plan = diff(table, items)
        info["plan"] = plan["summary"]
        info["items_in_books"] = len(items)
        store.data["last_plan"] = {k: plan[k] for k in ("create", "update", "reactivate", "conflict", "stale",
                                                          "duplicates", "manual", "summary")}
        if deferred:
            # The table is partial while quotes are still unread: plan, but
            # never write from half a window.
            info["applied"] = None
            info["held_for_deferred"] = True
        elif armed and (plan["create"] or plan["update"] or plan["reactivate"]):
            res = apply(plan, books, log=log, pause=min(pause, 0.4))
            info["applied"] = {k: len(v) for k, v in res.items()}
            info["errors"] = res["errors"][:20]
            store.data["last_apply"] = res
        else:
            info["applied"] = None
    except Exception as e:  # noqa: BLE001
        info["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        if log:
            log.exception("catalog sync run failed")
    finally:
        info["took"] = round(time.time() - t0, 1)
        store.note_run(info)
        try:
            store.save()
        except Exception as e:  # noqa: BLE001
            info["save_error"] = "%s: %s" % (type(e).__name__, e)
    return info
