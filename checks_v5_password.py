"""Regression: the /v5 word passwords let the floor in, and nobody else.

Danny asked for five shared word passwords on /v5. They are short dictionary
words, so the guard that matters is the guess RATE, not the secret. They are also
read from the environment and never written into the source, because
dvoskin/callboard is a PUBLIC repository -- a literal here would be a published
password, which is exactly what already happened to INGEST_KEY in
forwarder/gmail_to_ingest.gs.

Guards: a good password opens the page and the API; a bad one does not; guessing
is throttled per IP; and the comparison is constant-time so response timing does
not leak which of the five was closest.
"""
import os, sys, tempfile

sys.path.insert(0, __file__.rsplit('/', 1)[0])
os.environ["INGEST_API_KEY"] = "k"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="data_")
os.environ["V5_PASSWORDS"] = "ella,sally,ari,winner,anna"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")   # SSO on, so the gate is live

import app as A
A.V5_PASSWORDS = ("ella", "sally", "ari", "winner", "anna")
A.GOOGLE_CLIENT_ID = "test-client-id"
A.app.config["TESTING"] = True

fail = []
def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + str(detail)[:200]) if not cond else ""))
    if not cond: fail.append(name)

def fresh():
    A._v5_pw_tries.clear()
    return A.app.test_client()

# 1. every configured password works
for pw in ("ella", "sally", "ari", "winner", "anna"):
    c = fresh()
    r = c.post("/v5", data={"password": pw}, follow_redirects=False)
    ok("'%s' is accepted" % pw, r.status_code in (301, 302), r.status_code)

# 2. a wrong password does not
c = fresh()
r = c.post("/v5", data={"password": "sallyy"}, follow_redirects=False)
ok("a near miss is rejected", r.status_code == 401, r.status_code)
ok("rejection does not set the session", "v5_pw" not in (c.get_cookie("session") or ""),
   "cookie set on failure")

# 3. an unauthenticated GET shows the form, not the board
c = fresh()
r = c.get("/v5")
body = r.get_data(as_text=True)
ok("anonymous GET shows the password form", "Enter the password" in body, r.status_code)
ok("anonymous GET does not leak the board", "Sales Floor Scoreboard" in body
   and "Floor to clear" not in body, body[:120])

# 4. the API is closed until a password is given, and open after
c = fresh()
ok("API refuses before the password",
   c.get("/api/v5/report").status_code == 401,
   c.get("/api/v5/report").status_code)
c.post("/v5", data={"password": "ari"})
# Not 200: RingCentral is unconfigured in a test env, so the report answers 503
# ringcentral_not_configured. What is being tested is the AUTH gate, and 503
# means the request got past it -- asserting 200 would be asserting the fixture.
_after = c.get("/api/v5/report")
ok("API opens after the password", _after.status_code != 401,
   "%s %s" % (_after.status_code, _after.get_data(as_text=True)[:80]))

# 5. logging out closes it again
c.get("/v5/logout")
ok("logout closes the API again", c.get("/api/v5/report").status_code == 401,
   c.get("/api/v5/report").status_code)

# 6. guessing is throttled -- the real control for 3-6 character words
c = fresh()
codes = [c.post("/v5", data={"password": "nope%d" % i}).status_code for i in range(12)]
last = c.post("/v5", data={"password": "nope-final"})
ok("guessing is throttled", "Too many tries" in last.get_data(as_text=True),
   "no throttle after %d bad guesses" % len(codes))
ok("a correct password is refused while throttled",
   "Too many tries" in c.post("/v5", data={"password": "ella"}).get_data(as_text=True),
   "throttle bypassed by a correct guess")

# 7. no password may be hardcoded -- the repo is public
import io, subprocess
src = io.open("app.py", encoding="utf-8").read()
for pw in ("ella", "sally", "winner", "anna"):
    ok("'%s' is not hardcoded in app.py" % pw,
       ('"%s"' % pw) not in src and ("'%s'" % pw) not in src,
       "literal password in a public repo")
tracked = subprocess.run(["git", "grep", "-lE", r'V5_PASSWORDS *= *\("?ella'],
                         capture_output=True, text=True).stdout.strip()
ok("no tracked file assigns the passwords literally", not tracked, tracked)

# 8. the comparison must visit every candidate.
# A short-circuiting `any(...)` returns as soon as it matches, so the reply is
# marginally faster for an early password than a late one. Over HTTP, against a
# throttle, that difference is not exploitable -- but the code claims constant
# time, and a claim that is not tested is a claim that drifts. This checks the
# structure rather than the clock: no early exit inside the loop. (A timing
# assertion would be flaky and would prove nothing on a shared runner.)
import ast
fn = next(n for n in ast.walk(ast.parse(io.open("app.py", encoding="utf-8").read()))
          if isinstance(n, ast.FunctionDef) and n.name == "_v5_password_matches")
loops = [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]
ok("the compare loops over every candidate", len(loops) == 1, len(loops))
early = [n for l in loops for n in ast.walk(l) if isinstance(n, (ast.Return, ast.Break))]
ok("the loop has no early exit", not early,
   "returns/breaks inside the loop short-circuit the compare")
calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name) and n.func.id == "any"]
ok("no short-circuiting any()", not calls, "any() returns on first match")

print("\n%d failed" % len(fail))
sys.exit(1 if fail else 0)
