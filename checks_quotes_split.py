"""Regression: quotes sent is INCLUSIVE of invoiced, and invoiced is the subset.

"Quotes sent" counts every quote that left the office. "Invoiced" counts how many
of those converted -- a subset shown beside it, not carved out of it.

Carving it out was the first attempt and it failed in the same direction as the
original status=="sent" bug: the headline shrank as quotes succeeded. Adelita
Flowers on 2026-08-19 sent 16 and closed 4; reporting "sent 12" understated her
by exactly the quotes that worked. On 08-20 it is 10 sent, 4 invoiced.

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
    """Signature-tolerant on purpose.

    This fake used to pin each method's exact arguments, and when the real
    client grew a `stats=` keyword every quotes call raised TypeError. The
    fetch catches per-metric exceptions by design -- a Books outage must not
    blank the board -- so the error was swallowed, every bucket came back
    empty, and the check died on `KeyError: 'adelita flowers'` ten lines later.
    The runner then reported NO TALLY, i.e. nothing about quotes at all.

    *a/**k means a signature change can no longer break this. A change to WHICH
    method is called still fails, loudly, at the errors assertion below --
    which is why that assertion now runs FIRST.
    """
    configured = True
    def list_sent_estimates(self, *a, **k): return ROWS
    def list_sent_retainer_invoices(self, *a, **k): return []
    def list_retainer_payments(self, *a, **k): return []
    def list_retainers_sent(self, *a, **k): return []
    def list_retainers_paid_on(self, *a, **k): return []

A._books = FakeBooks()
A._v5_books_cache.clear()
# _v5_books is the non-blocking front door now -- it serves cache and kicks a
# background refresh, so on a cold cache it correctly returns nothing. The
# bucketing under test lives in the fetch itself.
by_agent, meta = A._v5_books_fetch("2026-08-19", "2026-08-19")

# FIRST, before anything dereferences a bucket. _v5_books_fetch swallows a
# per-metric failure on purpose, so an empty result is ambiguous: it means
# "no quotes" or "the call blew up". Asserting this here turns the second case
# into a sentence naming the method and the argument, instead of a KeyError on
# a name that has nothing to do with the cause.
ok("no Books error was raised", not meta.get("errors"), meta.get("errors"))

if meta.get("errors"):
    # Stop here rather than dying on a KeyError ten lines down. run_checks.sh
    # treats a suite with no tally line as "did not finish" and prints nothing
    # about what it was testing -- which is exactly how this sat red and
    # unread while the quotes split it guards was never actually checked.
    print("\n%d failed" % len(fail))
    sys.exit(1)

ade = by_agent[A._norm_name("Adelita Flowers")]
cha = by_agent[A._norm_name("Charlotte Mckay")]

ok("Adelita sent is 16 -- inclusive of the 4 invoiced", ade["quotes_sent"] == 16, ade)
ok("Adelita invoiced is 4", ade["quotes_invoiced"] == 4, ade)
ok("invoiced is a SUBSET, not an extra bucket",
   ade["quotes_invoiced"] < ade["quotes_sent"], ade)
ok("viewed and declined count as sent", cha["quotes_sent"] == 4, cha)   # 2 viewed + 1 declined + 1 signed
ok("signed counts as invoiced too", cha["quotes_invoiced"] == 1, cha)
ok("a converted quote is not lost from sent",
   cha["quotes_sent"] >= cha["quotes_invoiced"], cha)

# the template must render both, and must not resurrect the merged label
import io
tpl = io.open("templates/scoreboard_v5.html", encoding="utf-8").read()
# What matters is that each figure is BOUND into the per-agent panel, not how
# the cell is spelled. Pinning the exact call text made this fail the moment
# the panel was rebuilt to carry retainers too (f8fe33d) -- a rename reported
# as a missing metric, which is a false alarm that costs more than it catches.
ok("panel binds quotes sent", "bk.quotes_sent" in tpl)
ok("panel binds quotes invoiced", "bk.quotes_invoiced" in tpl)
# Retainers paid is the figure Danny found wrong on 2026-10-05; it reaching the
# panel at all is worth holding onto now that it is being changed.
ok("panel binds retainers paid", "bk.retainers_paid" in tpl)
ok("merged Quotes cell is gone", "cell2('Quotes', w(bk.quotes_sent)" not in tpl)

# ---- the seam between bucketing and template -------------------------------
# quotes_invoiced was computed correctly and rendered correctly, and still came
# out undefined: an explicit key whitelist in the report route dropped it on the
# way through. Tests on both SIDES of that projection were green. Structure, not
# spelling, so this is an AST check rather than a grep.
import ast
tree = ast.parse(io.open("app.py", encoding="utf-8").read())

bucket_keys, projected = set(), set()

# Scope to _v5_books' own bucket(): app.py has other setdefault buckets (call
# counters) whose keys are nothing to do with Books.
v5books = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_v5_books_fetch")
for node in ast.walk(v5books):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault" and len(node.args) == 2
            and isinstance(node.args[1], ast.Dict)):
        for k in node.args[1].keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                bucket_keys.add(k.value)

# The projection is the dict-comp that assigns a["books"].
for node in ast.walk(tree):
    if (isinstance(node, ast.Assign) and isinstance(node.value, (ast.DictComp, ast.IfExp))):
        dc = node.value.body if isinstance(node.value, ast.IfExp) else node.value
        tgt = node.targets[0]
        is_books = (isinstance(tgt, ast.Subscript)
                    and isinstance(getattr(tgt, "slice", None), ast.Constant)
                    and tgt.slice.value == "books")
        if is_books and isinstance(dc, ast.DictComp) \
                and isinstance(dc.generators[0].iter, ast.Tuple):
            for el in dc.generators[0].iter.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    projected.add(el.value)

counters = bucket_keys - {"display"}
missing = counters - projected
ok("projection carries every counter bucket() defines", not missing,
   "dropped on the way to the page: %s" % sorted(missing))
ok("the AST check found both sides", bool(counters) and bool(projected),
   "counters=%s projected=%s" % (sorted(counters), sorted(projected)))

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
