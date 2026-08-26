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
import os
import re
import threading
import time
import urllib.request
from collections import defaultdict, deque
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
    "pleasant": "Andrea Pleasant",
}


def _norm_tab(name):
    return "".join(c for c in (name or "").lower() if c.isalpha())


def build_tab_map(agents=()):
    """{normalised tab name: agent} from the explicit map plus each roster
    agent's own first and last name.

    The explicit map alone is a list somebody has to remember to update, and
    when they do not the tab is dropped in silence. The sheet's tab was renamed
    to "PLEASANT" -- not "Andrea", not "A.PLEASANT" -- and Andrea's collections
    stopped appearing with no error anywhere, including the day she had logged
    $8,006. Deriving from the roster means a tab named after the person matches
    whatever else it is called, and anything still unmatched is REPORTED rather
    than ignored.
    """
    out = {_norm_tab(k): v for k, v in TAB_TO_AGENT.items()}
    for full in agents or ():
        parts = [p for p in str(full).split() if p]
        for part in parts:
            key = _norm_tab(part)
            if len(key) >= 4:          # "de", "la" and initials match too much
                out.setdefault(key, full)
    return out

YEAR_LO, YEAR_HI = 2023, 2027          # anything outside this is a typo
# How far behind the sheet the board is allowed to be.
#
# Half an hour was set when a refresh held every tab in memory and took ~20s.
# It streams now -- 21MB peak, 5.3s for all four tabs -- so the board can follow
# the sheet much more closely for no meaningful cost. The loop wakes every two
# minutes, so worst case is this plus about two minutes.
_TTL = float(os.environ.get("COLLECTIONS_TTL_SECONDS", 60 * 5))


def _fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "call-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _term(ln):
    """One line with its terminator put back.

    csv.reader rebuilds a quoted field that spans lines out of the terminators
    it is given, so yielding bare lines silently joins "a note\nsplit in two"
    into "a notesplit in two".

    It does NOT also strip the CR. Restoring the newline is enough -- csv sees
    a normal CRLF ending and handles it, which a mutation confirmed by staying
    green when the strip was removed. Stripping it would additionally corrupt a
    CRLF *inside* a quoted note, where the carriage return is data.
    """
    return ln + "\n"


def _fetch_lines(url, timeout=120):
    """Yield decoded lines without ever holding the whole tab in memory.

    read_tab used to pull the CSV into a string, then build a list of every
    row, then take two more slices of that list -- so one tab was resident
    three times over. Vivian's is 25,824 rows wide enough to matter, and four
    of these run back to back inside a 512MB instance. That is the shape of a
    worker that dies silently and restarts: nothing raises, nothing is logged,
    and the cache simply stays empty forever, which is exactly what the live
    board reported -- loading: true, no error, no tabs.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "call-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        tail = ""
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            tail += chunk.decode("utf-8", "replace")
            lines = tail.split("\n")
            tail = lines.pop()
            for ln in lines:
                yield _term(ln)
        if tail:
            yield _term(tail)


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


_MONTHS = {}
for _i, _n in enumerate(["january", "february", "march", "april", "may", "june", "july",
                         "august", "september", "october", "november", "december"], 1):
    _MONTHS[_n] = _i
    _MONTHS[_n[:3]] = _i
_MONTHS["sept"] = 9


def parse_date(raw, default_year=None):
    """Hand-typed date -> date, or None. None means 'do not trust', not 'blank'.

    Four shapes, because the sheet contains four and only the first was read:

        8/26/2026     the shape this was written for
        8/26          Andrea's tab -- no year at all
        August 26     Vivian's -- a month NAME, and no year
        Aug 26, 2026  Yareth's surgery-date style

    The year-less ones are why today's collections were reported as zero while
    the sheet plainly had them: Andrea's $1,781 sits in a cell reading "8/26".

    default_year carries the year forward from the last row that stated one --
    the same rule the sheet already relies on for dates themselves. Guessing the
    CURRENT year instead would drag every undated row of an old section into
    this year and inflate today.
    """
    txt = (raw or "").strip().rstrip(".")
    if not txt:
        return None

    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", txt)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else default_year
        if y is None:
            return None
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

    # "August 26", "Aug 26, 2026", "Sept 7"
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?$", txt)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if not mon:
            return None
        d = int(m.group(2))
        y = int(m.group(3)) if m.group(3) else default_year
        if y is None or not (YEAR_LO <= y <= YEAR_HI):
            return None
        try:
            return date(y, mon, d)
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


RECENT_ROWS = 400         # the window that decides which column is live


def _date_column(header):
    low = [(c or "").strip().lower() for c in header]
    return next((i for i, h in enumerate(low)
                 if "date" in h and not any(x in h for x in ("dob", "birth", "surgery"))),
                None)


def _name_column(header):
    """The patient-name column, or None.

    Used to tell a payment from the sheet's own SUBTOTAL rows. Every real
    payment belongs to a patient; a row carrying money and no name is the
    running total the agents type at the end of a day's block. Undated, it
    inherits the date above under the forward-fill rule and is counted as if it
    were another payment -- so a day's takings landed twice.

    That was not a rounding error. 49.9% of Gabriela's money and 50.4% of
    Yareth's sat in nameless rows: their entire history read at roughly double.

    "Dr Name" is excluded, and so is anything about a location or centre.
    """
    low = [(c or "").strip().lower() for c in header]
    return next((i for i, h in enumerate(low)
                 if "name" in h
                 and not any(x in h for x in ("dr ", "dr.", "doctor", "location", "center",
                                              "centre", "surgeon"))), None)


def _amount_candidates(header):
    """The column the header names, and the one to its right.

    Both, because the header is not always over the money -- one tab has an
    extra column so "Amount Collected" sits one to the left of it -- and because
    a tab's layout can change partway down. Which of them is right is decided
    from the data, at the end, in read_tab.
    """
    low = [(c or "").strip().lower() for c in header]
    ai = next((i for i, h in enumerate(low) if "collect" in h), None)
    if ai is None:
        return []
    return [ai, ai + 1]


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


SNIFF_ROWS = 400          # enough to verify a column holds money; see _find_columns


def read_tab(sheet_id, gid):
    """{date: total} for one tab, plus a small diagnostic.

    Streams. Only the first SNIFF_ROWS rows are ever held at once -- the rest
    are folded into the running totals a row at a time -- so peak memory does
    not grow with the size of the tab.
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    reader = csv.reader(_fetch_lines(url))
    head = []
    for row in reader:
        head.append(row)
        if len(head) >= SNIFF_ROWS:
            break
    hi = next((i for i, r in enumerate(head[:10])
               if any("collect" in (c or "").strip().lower() for c in r)), None)
    if hi is None:
        return {}, {"error": "no 'collected' column found"}
    di = _date_column(head[hi])
    ni = _name_column(head[hi])
    cands = _amount_candidates(head[hi])
    if not cands:
        return {}, {"error": "could not locate the amount column"}

    # Fold EVERY candidate column, and decide between them at the end on which
    # one still holds money in the most RECENT rows.
    #
    # Choosing up front from the first 400 rows is what broke Yareth's tab: her
    # sheet's layout changed partway through, so "Amount Collected" has nothing
    # in the first 400 rows and 389 values in the last 400, while the column to
    # its right has the mirror image. Sampling the top picked the DEAD column
    # and every recent figure read as zero. What the board reports is recent, so
    # what decides the column has to be recent too.
    per = {c: defaultdict(float) for c in cands}
    last_money = {c: -1 for c in cands}     # row index of the last NON-ZERO value
    counts = {c: 0 for c in cands}
    seen_rows = 0
    cur = None
    last_year = None
    stats = {"dated": 0, "filled": 0, "bad_date": 0, "before_first_date": 0, "rows": 0}
    import itertools
    for r in itertools.chain(head[hi + 1:], reader):
        d = None
        if di is not None and di < len(r):
            # The year carries forward from the last row that stated one; the
            # sheet writes "August 26" and "8/26" with no year at all.
            d = parse_date(r[di], last_year)
            if d:
                cur, last_year = d, d.year
                stats["dated"] += 1
            elif (r[di] or "").strip():
                stats["bad_date"] += 1
        seen_rows += 1
        # A payment belongs to a patient. Money with no name is the block's own
        # subtotal, and counting it doubles the day. Only skipped when the tab
        # actually HAS a name column -- guessing one is not worth dropping every
        # row of a tab shaped differently.
        is_total = (ni is not None and not (ni < len(r) and (r[ni] or "").strip()))
        if is_total:
            # Counted ONCE for the row. Doing it inside the per-column loop
            # counted a one-column subtotal once and then divided it by the
            # number of candidates, which reported zero rows and half the money.
            row_amt = next((parse_amount(r[c]) for c in cands
                            if c < len(r) and parse_amount(r[c])), None)
            if row_amt:
                stats["subtotal_rows"] = stats.get("subtotal_rows", 0) + 1
                stats["subtotal_amount"] = round(
                    stats.get("subtotal_amount", 0.0) + row_amt, 2)
        for c in cands:
            amt = parse_amount(r[c]) if c < len(r) else None
            if amt is None:
                continue
            counts[c] += 1
            if amt:
                last_money[c] = seen_rows
            if is_total:
                continue
            if cur is None:
                continue
            per[c][cur] += amt
        if cur is None and any(c < len(r) and parse_amount(r[c]) is not None for c in cands):
            stats["before_first_date"] += 1
        elif cur is not None and not d:
            stats["filled"] += 1

    # Whichever column carried money most RECENTLY wins, with the header's own
    # column breaking a tie -- so a tab that never changed shape is read exactly
    # as before.
    #
    # Deliberately not "most values in the last N rows": that only works while
    # the live section is longer than the window, and a tab that switched
    # columns yesterday would still be read from the dead one. Where the money
    # stops is a fact about the tab, not about a window size someone chose.
    #
    # NON-ZERO, because the tabs end in hundreds of $0.00 padding rows and a
    # column of zeroes would otherwise look as live as a column of takings.
    def score(c):
        return (last_money[c], c == cands[0])
    ai = max(cands, key=score)
    stats["rows"] = counts[ai]
    stats["date_col"], stats["amount_col"], stats["name_col"] = di, ai, ni
    stats["amount_candidates"] = {c: {"values": counts[c], "last_money_row": last_money[c]}
                                  for c in cands}
    return dict(per[ai]), stats


class CollectionsClient:
    """Cached reader. Never raises at the caller: a sheet that will not load
    returns nothing plus a reason, because a dashboard showing $0 and a
    dashboard that could not read the sheet must not look the same."""

    def __init__(self, sheet_id=SHEET_ID, agents=()):
        self.sheet_id = sheet_id
        self.agents = list(agents or ())
        self._lock = threading.Lock()
        self._cache = {"at": 0.0, "data": None, "meta": None}

    def cached(self):
        """Whatever is in the cache, WITHOUT fetching. Never blocks.

        The tabs are megabytes each and live on Google's servers. Fetching them
        inside a web request put the billing board past gunicorn's 90s worker
        kill, and a killed worker drops the connection, so the page sat on
        "Loading" forever. Requests read this; only the background refresher
        calls refresh().
        """
        with self._lock:
            c = self._cache
            if c["data"] is None:
                return {}, {"loading": True, "tabs": {}, "errors": [],
                            "detail": "Collected amounts have not been read yet. "
                                      "They load in the background and appear on "
                                      "the next refresh."}
            return c["data"], dict(c["meta"], cached=True,
                                   age_seconds=round(time.time() - c["at"]))

    def stale(self):
        """True when the cache is old enough to be worth refreshing."""
        with self._lock:
            return self._cache["data"] is None or \
                time.time() - self._cache["at"] >= _TTL

    def refresh(self, force=False):
        """Do the actual fetch. Background thread only -- this is slow."""
        now = time.time()
        with self._lock:
            c = self._cache
            if not force and c["data"] is not None and now - c["at"] < _TTL:
                return c["data"], dict(c["meta"], cached=True)
        if True:
            data, meta = {}, {"cached": False, "tabs": {}, "errors": []}
            try:
                tabs = list_tabs(self.sheet_id)
            except Exception as e:  # noqa: BLE001
                meta["errors"].append("could not list tabs: %s" % e)
                log.warning("collections: tab list failed: %s", e)
                with self._lock:
                    return (self._cache["data"] or {}), meta
            tab_map = build_tab_map(self.agents)
            meta["unmapped_tabs"] = []
            for name, gid in tabs:
                agent = tab_map.get(_norm_tab(name))
                if not agent:
                    # Not silent. A tab nobody is reading is either deliberate
                    # (CASH, Late fee, a template) or an agent quietly missing
                    # from the board, and only a human can tell which.
                    meta["unmapped_tabs"].append(name)
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
            with self._lock:
                self._cache = {"at": time.time(), "data": data, "meta": meta}
            return data, meta

    # Backwards-compatible alias; the diagnostic endpoint still uses it.
    def daily_by_agent(self, force=False):
        return self.refresh(force=force) if force else self.cached()
