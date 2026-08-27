"""Collections sit behind a second word the hub password does not open.

Every other board names talk time and call counts. The billing board also
carries what each agent collected and what the practice took in, and the hub
word gets handed round the floor.

The gate is only worth anything if it covers the DATA as well as the page: the
figures are one query string away otherwise, and /api/v6/collections is the raw
form of exactly what is being protected.

Unset means unset -- with no BILLING_PASSWORDS the board behaves as it did,
rather than locking everyone out of a working page the moment this deploys.

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

    # Hub word alone: the other boards open, billing does not, and neither does
    # its data.
    c = _client()
    cases += [
        ("scheduling opens", BOARD in c.get("/scheduling").get_data(as_text=True), True),
        ("customer service opens",
         BOARD in c.get("/customer-service").get_data(as_text=True), True),
        ("billing page is gated", BOARD in c.get("/billing").get_data(as_text=True), False),
        ("...and asks for a password",
         "Billing" in c.get("/billing").get_data(as_text=True), True),
        ("billing DATA is gated",
         c.get("/api/v6/report?team=billing").status_code, 401),
        ("collections DATA is gated", c.get("/api/v6/collections").status_code, 401),
        # The other teams' data must stay open -- this is one board's door, not
        # a new gate on everything.
        ("scheduling data still open",
         c.get("/api/v6/report?team=scheduling").status_code != 401, True),
    ]

    # With the word, everything opens.
    c2 = _client()
    r = c2.post("/billing", data={"password": "biller"})
    cases += [
        ("correct word accepted", r.status_code, 302),
        ("billing page opens", BOARD in c2.get("/billing").get_data(as_text=True), True),
        ("billing data opens",
         c2.get("/api/v6/report?team=billing").status_code != 401, True),
        ("collections opens", c2.get("/api/v6/collections").status_code != 401, True),
    ]

    # A wrong word opens nothing, and the hub word is NOT the billing word.
    c3 = _client()
    r3 = c3.post("/billing", data={"password": "hubword"})
    cases += [
        ("hub word is not the billing word", r3.status_code, 401),
        ("...and the board stays shut",
         BOARD in c3.get("/billing").get_data(as_text=True), False),
    ]

    # UNSET: the board must behave exactly as before.
    import importlib
    os.environ["BILLING_PASSWORDS"] = ""
    importlib.reload(appmod)
    c4 = _client()
    cases += [
        ("unset -> page open", BOARD in c4.get("/billing").get_data(as_text=True), True),
        ("unset -> data open",
         c4.get("/api/v6/report?team=billing").status_code != 401, True),
    ]
    os.environ["BILLING_PASSWORDS"] = "biller"
    importlib.reload(appmod)

    for label, got, want in cases:
        ok = got == want
        print("  %-34s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
