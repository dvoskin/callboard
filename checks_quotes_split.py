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
               if isinstance(n, ast.FunctionDef) and n.name == "_v5_books")
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
