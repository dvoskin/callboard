"""ONE login. The dashboard's own gate -- the word password or a Google session
-- is the only door; billing's figures are not behind a second word.

Danny, 2026-10-07: "Make it just one login screen". Until then collections sat
behind BILLING_PASSWORDS, a second word the hub word did not open, and the
dashboard asked twice. Now a viewer who has passed the first gate sees billing
and its data; a request that passed no gate still gets nothing -- the DATA is
covered as well as the page, because the figures are one query string away
otherwise. BILLING_PASSWORDS is kept only so an existing billing_pw session
keeps working and share tokens behave as before.

Run with no arguments.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["V5_PASSWORDS"] = "boardword"
os.environ["V7_PASSWORDS"] = "hubword"
os.environ["BILLING_PASSWORDS"] = "biller"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402

BOARD = 'id="out"'          # only the real board markup carries this


def _client(word="hubword"):
    appmod.app.config["TESTING"] = True
    appmod._v5_pw_tries.clear()
    c = appmod.app.test_client()
    c.post("/v7", data={"password": word})
    return c


def run():
    fails = 0
    cases = []
    # The hub word alone opens everything, billing included -- one door.
    c = _client()
    cases += [
        ("scheduling opens", BOARD in c.get("/scheduling").get_data(as_text=True), True),
        ("customer service opens",
         BOARD in c.get("/customer-service").get_data(as_text=True), True),
        ("billing page opens on the hub word alone",
         BOARD in c.get("/billing").get_data(as_text=True), True),
        ("no second password page is shown",
         "Enter the password" in c.get("/billing").get_data(as_text=True), False),
        ("billing DATA opens", c.get("/api/v6/report?team=billing").status_code != 401, True),
        ("collections DATA opens", c.get("/api/v6/collections").status_code != 401, True),
        ("the dashboard link opens on the same word",
         BOARD in c.get("/billing-surgical").get_data(as_text=True), True),
        ("scheduling data still open",
         c.get("/api/v6/report?team=scheduling").status_code != 401, True),
    ]
    # No login at all: nothing opens, data included.
    appmod.app.config["TESTING"] = True
    c0 = appmod.app.test_client()
    cases += [
        ("without any login the billing page shows no board",
         BOARD in c0.get("/billing").get_data(as_text=True), False),
        ("without any login billing DATA is refused",
         c0.get("/api/v6/report?team=billing").status_code, 401),
        ("without any login collections DATA is refused",
         c0.get("/api/v6/collections").status_code, 401),
        ("without any login the dashboard shows its sign-in, titled as itself",
         "Surgical Coordinator Dashboard" in c0.get("/billing-surgical").get_data(as_text=True)
         and BOARD not in c0.get("/billing-surgical").get_data(as_text=True), True),
    ]
    # A share token still grants the billing board without a login.
    _tok = appmod._v6_token_ok
    appmod._v6_token_ok = lambda: True
    try:
        cases += [
            ("a share token still opens billing data",
             c0.get("/api/v6/report?team=billing&k=x").status_code != 401, True),
        ]
    finally:
        appmod._v6_token_ok = _tok
    for label, got, want in cases:
        ok = got == want
        fails += 0 if ok else 1
        print("  %-58s want %-6s got %-6s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
