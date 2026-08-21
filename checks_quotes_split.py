"""Regression: quotes sent and quotes invoiced are counted apart.

An invoiced quote was signed -- a different fact from one still sitting with the
customer. Merging both into a single "Quotes" number hid which of the two had
actually moved: Adelita Flowers on 2026-08-19 is 12 sent and 4 invoiced, and the
merged 16 read as though nobody had closed anything.

Exercises the real _v5_books bucketing, not a copy of it.
"""
import os, sys, tempfile

sys.path.insert(0, __file__.rsplit('/', 1)[0])
os.environ.setdefault("INGEST_API_KEY", "test-key")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="data_"))
import app as A

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:200]) if not cond else ""))
    if not cond: fail.append(name)

# Adelita's real 2026-08-19 shape, straight from Books.
ROWS = ([{"salesperson_name": "Adelita Flowers", "status": "sent"}] * 12 +
        [{"salesperson_name": "Adelita Flowers", "status": "invoiced"}] * 4 +
        [{"salesperson_name": "Charlotte Mckay", "status": "viewed"}] * 2 +
        [{"salesperson_name": "Charlotte Mckay", "status": "declined"}] * 1 +
        [{"salesperson_name": "Charlotte Mckay", "status": "signed"}] * 1)

class FakeBooks:
    configured = True
    def list_sent_estimates(self, a, b, c=None): return ROWS
    def list_sent_retainer_invoices(self, a, b, c=None): return []
    def list_retainer_payments(self, a, b): return []

A._books = FakeBooks()
A._v5_books_cache.clear()
by_agent, meta = A._v5_books("2026-08-19", "2026-08-19")

ade = by_agent[A._norm_name("Adelita Flowers")]
cha = by_agent[A._norm_name("Charlotte Mckay")]

ok("Adelita sent is 12, not 16", ade["quotes_sent"] == 12, ade)
ok("Adelita invoiced is 4", ade["quotes_invoiced"] == 4, ade)
ok("the two are not merged", ade["quotes_sent"] + ade["quotes_invoiced"] == 16, ade)
ok("viewed counts as sent", cha["quotes_sent"] == 3, cha)          # 2 viewed + 1 declined
ok("signed counts as invoiced", cha["quotes_invoiced"] == 1, cha)
ok("no Books error was raised", not meta.get("errors"), meta.get("errors"))

# the template must render both, and must not resurrect the merged label
import io
tpl = io.open("templates/scoreboard_v5.html", encoding="utf-8").read()
ok("panel shows Quotes sent", "cell2('Quotes sent', w(bk.quotes_sent)" in tpl)
ok("panel shows Invoiced", "cell2('Invoiced', w(bk.quotes_invoiced || 0)" in tpl)
ok("merged Quotes cell is gone", "cell2('Quotes', w(bk.quotes_sent)" not in tpl)

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
