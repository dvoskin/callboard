"""The retainer scan must know whether it reached the window.

Books cannot filter retainers on last_payment_date, so "retainers paid" is a
newest-modified-first scan of EVERY retainer, filtered afterwards and capped at
a record budget. That makes two outcomes identical from the outside:

  * the scan paged back past the window and found everything, and
  * the scan ran out of budget halfway and found some of it.

Both return a short list. One is a count; the other is a floor. Before this,
the second silently printed as the first.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import books_client as bc  # noqa: E402


class _Resp:
    status_code = 200
    ok = True

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _client(pages):
    """A client whose HTTP layer serves `pages` (1-indexed) of retainers."""
    c = bc.BooksClient()
    c.client_id = c.client_secret = c.refresh_token = c.org_id = "x"
    c._headers = lambda: {}

    def fake_get(url, headers=None, params=None, timeout=None):
        p = params.get("page", 1)
        rows = pages.get(p, [])
        return _Resp({"retainerinvoices": rows,
                      "page_context": {"has_more_page": p < max(pages)}})
    c._orig_get = bc.requests.get
    bc.requests.get = fake_get
    return c


def _row(day, n):
    return {"retainerinvoice_id": "R%s-%s" % (day, n), "status": "paid",
            "total": 100, "last_payment_date": day + "T00:00:00+0000",
            "last_modified_time": day + "T12:00:00+0000"}


def _eq(label, got, want, fails):
    ok = got == want
    print("  %-52s %s (got %r)" % (label, "ok" if ok else "MISMATCH", got))
    return fails + (0 if ok else 1)


def case_stops_once_past_the_window():
    """Page 2 predates the window, so page 3 must never be requested."""
    f = 0
    pages = {
        1: [_row("2026-09-20", i) for i in range(200)],
        2: [_row("2026-08-01", i) for i in range(200)],   # older than the window
        3: [_row("2026-07-01", i) for i in range(200)],
    }
    c = _client(pages)
    try:
        st = {}
        out = c.list_retainers_paid_on("2026-09-01", "2026-09-30", max_pages=8, stats=st)
    finally:
        bc.requests.get = c._orig_get
    f = _eq("stopped after the first page past the window", st.get("pages"), 2, f)
    f = _eq("reports that it reached back", st.get("reached_back"), True, f)
    f = _eq("not flagged truncated", st.get("truncated"), False, f)
    f = _eq("only in-window retainers counted", len(out), 200, f)
    return f


def case_truncation_is_declared():
    """Budget runs out while every row is still newer than the window."""
    f = 0
    # Every page is inside the window, and there is always another page.
    pages = {p: [_row("2026-09-20", "%s-%s" % (p, i)) for i in range(200)]
             for p in range(1, 12)}
    c = _client(pages)
    try:
        st = {}
        # max_pages=2 -> a 400-record budget against 2,200 rows.
        c.list_retainers_paid_on("2026-09-01", "2026-09-30", max_pages=2, stats=st)
    finally:
        bc.requests.get = c._orig_get
    f = _eq("declares itself truncated", st.get("truncated"), True, f)
    f = _eq("and does NOT claim it reached back", st.get("reached_back"), False, f)
    return f


def case_inferred_day_is_counted():
    """A row dated only by last_modified_time is an inference, and is counted."""
    f = 0
    r = _row("2026-09-20", 1)
    r["last_payment_date"] = ""          # payment not stamped yet
    pages = {1: [r], 2: [_row("2026-08-01", 9)]}
    c = _client(pages)
    try:
        st = {}
        out = c.list_retainers_paid_on("2026-09-01", "2026-09-30", max_pages=8, stats=st)
    finally:
        bc.requests.get = c._orig_get
    f = _eq("the row still counts", len(out), 1, f)
    f = _eq("but is reported as inferred", st.get("inferred_day"), 1, f)

    # Negative half: a properly stamped payment is NOT reported as inferred.
    pages2 = {1: [_row("2026-09-20", 1)], 2: [_row("2026-08-01", 9)]}
    c2 = _client(pages2)
    try:
        st2 = {}
        c2.list_retainers_paid_on("2026-09-01", "2026-09-30", max_pages=8, stats=st2)
    finally:
        bc.requests.get = c2._orig_get
    f = _eq("a stamped payment is not 'inferred'", st2.get("inferred_day"), 0, f)
    return f


def run():
    total = 0
    for title, fn in [
        ("the scan stops once it is past the window", case_stops_once_past_the_window),
        ("a truncated scan says so", case_truncation_is_declared),
        ("a day inferred from last_modified is counted", case_inferred_day_is_counted),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
