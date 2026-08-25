"""Daily collected amounts per billing agent, from the shared Google Sheet.

The sheet is one tab per agent and every tab was built by a different person, so
nothing about the layout is dependable:

    Vivian      "Date"          "Amount Collected"
    Yareth      "Date"          "Amount Collected"  <- header is one column left
                                                       of the money
    Gabriela    "Date (Today)"  "Amount Collected"
    Andrea      "Date (Today)"  "Amount Collected"  amounts sometimes bare (1000)
    A.PLEASANT  "DATE:"         "COLLECTED"         dates like 8/24/26

So columns are detected per tab rather than assumed, and the detection is
checked against the data before it is trusted.

WHAT IS DELIBERATELY NOT READ
-----------------------------
The tabs carry patient name, date of birth, surgery date and medical clearance.
None of that is needed to answer "how much did this agent collect today", so
none of it is read past the column scan and none of it leaves this module. The
dashboard only ever sees {agent: {date: amount}}.

DATE HANDLING
-------------
Danny's rule: a row with no date belongs to the date above it -- the agents type
the date once and list that day's payments underneath. Forward-fill only carries
a date that PARSED; a typo must not silently claim the rows beneath it.

Dates are hand-typed and mix MM/DD/YYYY with DD/MM/YYYY, so a first component
over 12 disambiguates and anything else is read as US. Years outside a sane
window are typos (the sheet contains 0205 and 3824) and are dropped rather than
forward-filled.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime

log = logging.getLogger(__name__)

SHEET_ID = "1ozEPsEche4ty2CbNABQjeQa8zqTb25GRTJg1uMiR2OI"

# Tab name -> the agent it belongs to on the billing board. A tab that is not
# listed here is ignored, which keeps CASH / Late fee / templates out.
TAB_TO_AGENT = {
    "vivian": "Vivian Martinez",
    "yareth": "Yareth Pavon",
    "gabriela": "Gabriela Maldonado",
    "andrea": "Andrea Pleasant",
    "a.pleasant": "Andrea Pleasant",
}

YEAR_LO, YEAR_HI = 2023, 2027          # anything outside this is a typo
_TTL = float(60 * 30)                   # the sheet is megabytes; half an hour


def _fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "call-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def list_tabs(sheet_id=SHEET_ID):
    """[(name, gid)] for every tab, read from the published HTML view."""
    html = _fetch(f"https://docs.google.com/spreadsheets/d/{sheet_id}/htmlview")
    pairs = re.findall(r'\{name:\s*"(.*?)",\s*pageUrl:.*?gid=(\d+)', html) or \
            re.findall(r'name:\s*"([^"]+)".{0,200}?gid=(\d+)', html, re.S)
    out, seen = [], set()
    for n, g in pairs:
        if g in seen:
            continue
        seen.add(g)
        out.append((n.strip(), g))
    return out


def parse_date(raw):
    """Hand-typed date -> date, or None. None means 'do not trust', not 'blank'."""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", (raw or "").strip())
    if not m:
        return None
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    if not (YEAR_LO <= y <= YEAR_HI):
        return None
    try:
        if a > 12 and b <= 12:
            return date(y, b, a)          # unambiguously DD/MM
        if a <= 12:
            return date(y, a, b)          # MM/DD, the sheet's usual habit
    except ValueError:
        return None
    return None


def parse_amount(raw):
    """'$6,350' / '1000' / '' -> float or None."""
    v = re.sub(r"[^0-9.\-]", "", raw or "")
    if v in ("", "-", ".", "--"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _find_columns(header, rows):
    """(date_index, amount_index) for one tab, verified against the data.

    The amount header is not always over the amount: Yareth's sheet has an extra
    column, so 'Amount Collected' sits one to the left of the money. Detection
    therefore proposes a column and then checks it actually holds numbers,
    stepping right if it does not.
    """
    low = [(c or "").strip().lower() for c in header]
    di = next((i for i, h in enumerate(low)
               if "date" in h and not any(x in h for x in ("dob", "birth", "surgery"))), None)
    ai = next((i for i, h in enumerate(low) if "collect" in h), None)
    if ai is None:
        return di, None
    # Does the proposed column actually carry money? Try it, then one right.
    for cand in (ai, ai + 1):
        hits = sum(1 for r in rows[:400]
                   if cand < len(r) and parse_amount(r[cand]) is not None)
        if hits >= 5:
            return di, cand
    return di, ai


def read_tab(sheet_id, gid):
    """{date: total} for one tab, plus a small diagnostic."""
    raw = _fetch(f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}")
    rows = list(csv.reader(io.StringIO(raw)))
    hi = next((i for i, r in enumerate(rows[:10])
               if any("collect" in (c or "").strip().lower() for c in r)), None)
    if hi is None:
        return {}, {"error": "no 'collected' column found"}
    di, ai = _find_columns(rows[hi], rows[hi + 1:])
    if ai is None:
        return {}, {"error": "could not locate the amount column"}

    per = defaultdict(float)
    cur = None
    stats = {"dated": 0, "filled": 0, "bad_date": 0, "before_first_date": 0, "rows": 0}
    for r in rows[hi + 1:]:
        if ai >= len(r):
            continue
        d = parse_date(r[di]) if (di is not None and di < len(r)) else None
        if d:
            cur = d
            stats["dated"] += 1
        elif di is not None and di < len(r) and (r[di] or "").strip():
            stats["bad_date"] += 1
        amt = parse_amount(r[ai])
        if amt is None:
            continue
        if cur is None:
            stats["before_first_date"] += 1
            continue
        if not d:
            stats["filled"] += 1          # Danny's rule: belongs to the date above
        per[cur] += amt
        stats["rows"] += 1
    stats["date_col"], stats["amount_col"] = di, ai
    return dict(per), stats


class CollectionsClient:
    """Cached reader. Never raises at the caller: a sheet that will not load
    returns nothing plus a reason, because a dashboard showing $0 and a
    dashboard that could not read the sheet must not look the same."""

    def __init__(self, sheet_id=SHEET_ID):
        self.sheet_id = sheet_id
        self._lock = threading.Lock()
        self._cache = {"at": 0.0, "data": None, "meta": None}

    def daily_by_agent(self, force=False):
        """({agent: {date: amount}}, meta)."""
        now = time.time()
        with self._lock:
            c = self._cache
            if not force and c["data"] is not None and now - c["at"] < _TTL:
                return c["data"], dict(c["meta"], cached=True)
            data, meta = {}, {"cached": False, "tabs": {}, "errors": []}
            try:
                tabs = list_tabs(self.sheet_id)
            except Exception as e:  # noqa: BLE001
                meta["errors"].append("could not list tabs: %s" % e)
                log.warning("collections: tab list failed: %s", e)
                return (c["data"] or {}), meta
            for name, gid in tabs:
                agent = TAB_TO_AGENT.get(name.strip().lower())
                if not agent:
                    continue
                try:
                    per, st = read_tab(self.sheet_id, gid)
                except Exception as e:  # noqa: BLE001
                    meta["errors"].append("%s: %s" % (name, e))
                    log.warning("collections: tab %s failed: %s", name, e)
                    continue
                meta["tabs"][name] = st
                if not per:
                    continue
                # Two tabs can feed one agent (Andrea has "Andrea" and
                # "A.PLEASANT"); sum them rather than letting one win.
                tgt = data.setdefault(agent, {})
                for d, v in per.items():
                    tgt[d] = tgt.get(d, 0.0) + v
            meta["agents"] = sorted(data)
            self._cache = {"at": now, "data": data, "meta": meta}
            return data, meta
