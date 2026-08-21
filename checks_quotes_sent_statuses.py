"""Regression: a quote that succeeded still counts as a quote sent.

list_sent_estimates filtered the Books API on status=="sent". An estimate stops
reading "sent" as soon as it moves on -- viewed, accepted, declined, expired, or
converted to an invoice -- so the board counted only the quotes that had gone
NOWHERE, and penalised a rep exactly when a quote worked.

Measured against Books for 2026-08-20: 125 estimates dated that day, 1 draft, so
124 actually left the office. The board showed 100. Every one of the 24 it
dropped had been invoiced. The loss was not uniform, so the RANKING was wrong
too -- Adelita Flowers read 6 against 10 sent, Alicia Mckenzie 2 against 5,
while Adriana Gentry (13, none converted) was untouched and stayed top.

Guards both halves: the counted states (sent, viewed, declined, invoiced,
signed -- Danny's explicit set) must count, and everything else must not. Also guards the paging trap -- filtering happens client-side, so the
loop must page on the API's has_more_page and not on the filtered batch size,
or one page of drafts would silently end the fetch.
"""
import sys, types

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import books_client as bc

# The counted set, given explicitly by Danny.
COUNTED = ["sent", "viewed", "declined", "invoiced", "signed"]
# Everything else Books can return must stay out until someone says otherwise.
NOT_COUNTED = ["draft", "pending_approval", "accepted", "expired",
               "approved", "rejected", "partially_invoiced", "pending_signature"]

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:200]) if not cond else ""))
    if not cond: fail.append(name)


def client_returning(pages):
    """pages: list of lists of status strings, one per API page."""
    calls = {"n": 0, "params": []}
    def _get(url, headers=None, params=None, timeout=None):
        i = calls["n"]; calls["n"] += 1
        calls["params"].append(dict(params or {}))
        batch = [{"estimate_id": "e%d_%d" % (i, j), "status": st,
                  "salesperson_name": "Rep %s" % st, "date": "2026-08-20"}
                 for j, st in enumerate(pages[i])] if i < len(pages) else []
        class R:
            status_code, ok = 200, True
            text = ""
            def json(self):
                return {"estimates": batch,
                        "page_context": {"has_more_page": i < len(pages) - 1}}
        return R()
    bc.requests = types.SimpleNamespace(get=_get, post=None)
    c = bc.BooksClient.__new__(bc.BooksClient)
    c.client_id, c.client_secret, c.refresh_token, c.org_id = "i", "s", "r", "773501182"
    c.base_url = "https://books/v3"
    c.cache_path = type("P", (), {"exists": staticmethod(lambda: False)})()
    c.last_source_was_cache = False
    c._headers = lambda: {}
    return c, calls


# 1. every counted state is counted
c, calls = client_returning([COUNTED])
rows = c.list_sent_estimates("2026-08-20", "2026-08-20")
got = sorted(r["status"] for r in rows)
ok("sent/viewed/declined/invoiced/signed all count", got == sorted(COUNTED), got)

# 2. an outcome of a sent quote still counts -- the actual defect
c, calls = client_returning([["sent", "invoiced", "declined", "viewed"]])
rows = c.list_sent_estimates("2026-08-20", "2026-08-20")
ok("invoiced/declined/viewed are not dropped", len(rows) == 4, len(rows))

# 3. everything outside the whitelist stays out
c, calls = client_returning([NOT_COUNTED + ["sent"]])
rows = c.list_sent_estimates("2026-08-20", "2026-08-20")
got = sorted(r["status"] for r in rows)
ok("uncounted statuses excluded", got == ["sent"], got)

# 4. the server-side status filter must be gone -- it is what caused the loss
c, calls = client_returning([["sent"]])
c.list_sent_estimates("2026-08-20", "2026-08-20")
ok("no server-side status=sent filter", "status" not in calls["params"][0],
   calls["params"][0])

# 5. paging follows has_more_page, not the filtered batch size
c, calls = client_returning([["draft", "accepted"], ["sent", "invoiced"]])
rows = c.list_sent_estimates("2026-08-20", "2026-08-20")
ok("a page filtered to empty does not end the fetch", len(rows) == 2, len(rows))
ok("both pages were requested", calls["n"] == 2, calls["n"])

# 6. the real 08-20 shape: 100 sent + 24 invoiced + 1 draft -> 124
c, calls = client_returning([["sent"] * 100 + ["invoiced"] * 24 + ["draft"]])
rows = c.list_sent_estimates("2026-08-20", "2026-08-20")
ok("2026-08-20 reproduces 124, not 100", len(rows) == 124, len(rows))

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
