"""Which RingCentral line has texted which phone number, and when. Never the text.

The call & text search (Danny, 2026-10-30: "add the texts sent from ringcentral
lines too"). RingCentral keeps texts per extension and has no account-wide
search, and this account has 102 texting lines: asking every line on every
search would be ~100 message-store reads against a budget of 50 a minute that
the boards share. So a background loop reads each line's new messages and keeps
only the pairing -- (patient number, line, first, last) -- and the search reads
the actual texts LIVE from just the lines this index names, usually one to
three.

Bodies are patient text and are deliberately not written to disk, the same
rule the boards' day snapshots follow (ringcx_client.fetch_extension_messages).

One writer: the call tracker runs a single gunicorn worker, and the loop is one
named thread in it. SQLite in WAL mode so a search can read while it writes.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
create table if not exists pairs (
  phone text not null,          -- the other party, last ten digits
  ext   text not null,          -- RingCentral extension id of our line
  first text not null,          -- ISO time of the earliest text seen
  last  text not null,          -- ISO time of the latest text seen
  primary key (phone, ext)
);
create index if not exists pairs_phone on pairs (phone, last);
create table if not exists lines (
  ext          text primary key,
  name         text,
  ext_no       text,
  numbers      text,            -- our numbers on this line, ten digits, comma separated
  synced_to    text,            -- read through this time (newest edge)
  filled_from  text,            -- read back to this time (oldest edge)
  last_error   text,
  updated      text
);
"""


def d10(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    return d[-10:] if len(d) >= 10 else ""


def _iso(t: datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class SmsIndex:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._db() as c:
            c.executescript(_SCHEMA)

    def _db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("pragma journal_mode=wal")
        return c

    # ---- the lines ----------------------------------------------------------
    def set_lines(self, lines: dict) -> int:
        """lines: {ext_id: {"name", "ext_no", "numbers": [10-digit, ...]}}. Keeps each
        line's progress; a line that left the directory stays, so its history still
        answers a search."""
        now = _iso(datetime.now(timezone.utc))
        with self._lock, self._db() as c:
            for ext, v in lines.items():
                c.execute("""insert into lines (ext, name, ext_no, numbers, updated) values (?,?,?,?,?)
                             on conflict(ext) do update set name=excluded.name, ext_no=excluded.ext_no,
                             numbers=excluded.numbers, updated=excluded.updated""",
                          (str(ext), v.get("name") or "", str(v.get("ext_no") or ""),
                           ",".join(sorted({d10(n) for n in v.get("numbers") or [] if d10(n)})), now))
        return len(lines)

    def lines(self) -> list[dict]:
        with self._db() as c:
            return [dict(r) for r in c.execute("select * from lines order by ext")]

    def line(self, ext) -> dict | None:
        with self._db() as c:
            r = c.execute("select * from lines where ext=?", (str(ext),)).fetchone()
            return dict(r) if r else None

    def mark(self, ext, synced_to=None, filled_from=None, error=None):
        sets, args = [], []
        if synced_to is not None:
            sets.append("synced_to=?"); args.append(_iso(synced_to))
        if filled_from is not None:
            sets.append("filled_from=?"); args.append(_iso(filled_from))
        sets.append("last_error=?"); args.append(error)
        with self._lock, self._db() as c:
            c.execute("update lines set %s where ext=?" % ", ".join(sets), (*args, str(ext)))

    # ---- the pairs ----------------------------------------------------------
    def record(self, ext, rows, own_numbers=()) -> int:
        """Fold message-store rows (fetch_extension_messages, without text) into the
        pairs. The other party is `to` on an outbound text and `from` on an inbound
        one; our own numbers are never a patient."""
        own = {d10(n) for n in own_numbers if d10(n)}
        seen = {}
        for r in rows or []:
            other = r.get("to_number") if (r.get("direction") or "") == "Outbound" else r.get("from_number")
            p = d10(other)
            t = r.get("start_time") or ""
            if not p or p in own or not parse_iso(t):
                continue
            t = _iso(parse_iso(t))
            lo, hi = seen.get(p, (t, t))
            seen[p] = (min(lo, t), max(hi, t))
        if not seen:
            return 0
        with self._lock, self._db() as c:
            for p, (lo, hi) in seen.items():
                c.execute("""insert into pairs (phone, ext, first, last) values (?,?,?,?)
                             on conflict(phone, ext) do update set
                               first=min(first, excluded.first), last=max(last, excluded.last)""",
                          (p, str(ext), lo, hi))
        return len(seen)

    def lines_for(self, phone, since: datetime | None = None) -> list[dict]:
        """Our lines that texted this number, latest first, optionally only those
        with a text at or after `since`."""
        p = d10(phone)
        if not p:
            return []
        q = ("select p.ext, p.first, p.last, l.name, l.ext_no from pairs p left join lines l on l.ext=p.ext "
             "where p.phone=?")
        args = [p]
        if since is not None:
            q += " and p.last >= ?"
            args.append(_iso(since))
        with self._db() as c:
            return [dict(r) for r in c.execute(q + " order by p.last desc", args)]

    def status(self) -> dict:
        with self._db() as c:
            n_lines = c.execute("select count(*) from lines").fetchone()[0]
            synced = c.execute("select count(*), min(synced_to) from lines where synced_to is not null").fetchone()
            filled = c.execute("select max(filled_from) from lines").fetchone()[0]   # the line filled least far back
            unfilled = c.execute("select count(*) from lines where filled_from is null").fetchone()[0]
            errs = c.execute("select count(*) from lines where last_error is not null").fetchone()[0]
            pairs = c.execute("select count(*) from pairs").fetchone()[0]
        return {"lines": n_lines, "lines_synced": synced[0], "oldest_sync": synced[1],
                "filled_back_to": filled if not unfilled else None, "lines_not_started": unfilled,
                "lines_with_error": errs, "pairs": pairs}
