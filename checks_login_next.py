"""Sign-in returns you to the page you came from (2026-10-20).

"Sign in with Google" on /my-day ended on the sales live board: /login sent a
signed-in person to "/" and /auth/callback always went to "/". Now a `next`
path rides /login?next= -> the session -> /auth/callback. Only a path on this
site is honoured: "//evil.com", "https://evil.com" and "/\\evil.com" are open
redirects and land on "/" instead.
Run: ./.venv/bin/python -B checks_login_next.py   (tally LAST)
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app as A  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


saved = (A.GOOGLE_CLIENT_ID, A.oauth.google.authorize_access_token, A.oauth.google.authorize_redirect,
         A.V5_PASSWORDS, A.V7_PASSWORDS)
try:
    A.GOOGLE_CLIENT_ID = "x"
    A.oauth.google.authorize_access_token = lambda *a, **k: {
        "userinfo": {"email": "jess@goalsplasticsurgery.com", "name": "Jess"}}
    A.oauth.google.authorize_redirect = lambda *a, **k: A.redirect("https://accounts.example.test/o")

    def round_trip(login_url):
        """/login?next=... then Google's callback; where does it land?"""
        c = A.app.test_client()
        c.get(login_url)
        c.get("/auth/google")
        r = c.get("/auth/callback")
        return r.status_code, r.headers.get("Location", "")

    st, loc = round_trip("/login?next=/my-day")
    ck("next is kept through Google: /login?next=/my-day lands on /my-day", st == 302 and loc.endswith("/my-day")
       and "//" not in loc.split("://", 1)[-1], loc)
    st, loc = round_trip("/login?next=%2Fmy-day%2F5212466000001538001%3Ffresh%3D1")
    ck("...a path with a query survives", loc.endswith("/my-day/5212466000001538001?fresh=1"), loc)
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "%2F%2Fevil.com", "evil.com", "javascript:alert(1)"):
        st, loc = round_trip("/login?next=" + bad)
        ck("open redirect refused: next=%s lands on /" % bad, loc in ("/", "http://localhost/"), loc)
    st, loc = round_trip("/login")
    ck("no next: lands on / (as before)", loc in ("/", "http://localhost/"), loc)

    c = A.app.test_client()
    c.get("/auth/google?next=/teams")
    loc = c.get("/auth/callback").headers.get("Location", "")
    ck("/auth/google?next= is honoured too", loc.endswith("/teams"), loc)

    # signed in already: /login goes to next, still never off-site
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"email": "jess@goalsplasticsurgery.com"}
    ck("signed in: /login?next=/my-day goes to /my-day",
       c.get("/login?next=/my-day").headers.get("Location", "").endswith("/my-day"), "signed-in")
    loc = c.get("/login?next=//evil.com").headers.get("Location", "")
    ck("signed in: /login?next=//evil.com goes to /", loc in ("/", "http://localhost/"), loc)

    ck("_safe_next: a path yes; //, scheme, backslash, control chars no",
       A._safe_next("/my-day") == "/my-day" and A._safe_next("//x") == "" and A._safe_next("https://x") == ""
       and A._safe_next("/\\x") == "" and A._safe_next("/a\nb") == "" and A._safe_next("") == "", "safe")

    # the doors pass the page they guard
    c = A.app.test_client()
    A.V5_PASSWORDS, A.V7_PASSWORDS = ("word",), ()
    html = c.get("/my-day").get_data(as_text=True)
    ck("the password page's Google link carries next=/my-day", 'href="/login?next=/my-day"' in html
       or 'href="/login?next=%2Fmy-day"' in html, [l for l in html.splitlines() if "Sign in with Google" in l])
    A.V5_PASSWORDS, A.V7_PASSWORDS = (), ()
    loc = A.app.test_client().get("/my-day").headers.get("Location", "")
    ck("no word password set: /my-day sends to /login?next=/my-day", "/login?next=%2Fmy-day" in loc, loc)
    loc = A.app.test_client().get("/").headers.get("Location", "")
    ck("login_required on / still sends to plain /login", loc.endswith("/login"), loc)
finally:
    (A.GOOGLE_CLIENT_ID, A.oauth.google.authorize_access_token, A.oauth.google.authorize_redirect,
     A.V5_PASSWORDS, A.V7_PASSWORDS) = saved

for e in errors:
    print("FAIL:", e)
print("checks_login_next: %d passed, %d failed" % (passed, len(errors)))
print("%d failed" % len(errors))
sys.exit(1 if errors else 0)
