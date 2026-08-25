"""The /v7 hub password must open the hub and NOTHING else.

The hub exists so a short, memorable word can get someone to a list of links.
/v5 and /v6 name individual employees and rank their performance, so they keep
their own door. That is only true if the v7 session flag is never accepted by
those routes -- and "I only added it in one place" is exactly the kind of claim
that stops being true six months later.

So this drives the real Flask app through a real session and asserts what each
credential actually opens. Both directions matter:
  * the hub word opens /v7 and is REFUSED by /v5 and /v6
  * the v5 word opens all three, because the stronger word implies the weaker

Run with no arguments.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set before importing app: both are read at module import.
os.environ["V5_PASSWORDS"] = "boardword"
os.environ["V7_PASSWORDS"] = "hubword"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")   # force the gate ON
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app as appmod  # noqa: E402


def _client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _login(c, where, word):
    return c.post(where, data={"password": word}, follow_redirects=False)


def case_hub_word_opens_only_the_hub():
    c = _client()
    _login(c, "/v7", "hubword")
    results = {
        "/v7": c.get("/v7").status_code,
        "/v5": c.get("/v5").status_code,
        "/v6": c.get("/v6").status_code,
        "/api/v6/report": c.get("/api/v6/report").status_code,
    }
    fails = 0
    # 200 on /v7 is the hub. On /v5 and /v6 a 200 is the PASSWORD PAGE, not the
    # board, so status alone cannot tell them apart -- check the body.
    hub = c.get("/v7").get_data(as_text=True)
    v5 = c.get("/v5").get_data(as_text=True)
    v6 = c.get("/v6").get_data(as_text=True)
    checks = [
        ("hub renders", "Goals Dashboards" in hub, True),
        ("hub says limited", "hub password" in hub, True),
        ("/v5 board REFUSED", "Sales Floor Scoreboard" in v5 and "id=\"out\"" in v5, False),
        ("/v6 board REFUSED", "Billing Team KPI Board" in v6 and "id=\"out\"" in v6, False),
        ("v6 API unauthorized", results["/api/v6/report"], 401),
    ]
    for label, got, want in checks:
        ok = got == want
        print("  %-24s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_board_word_opens_everything():
    c = _client()
    _login(c, "/v7", "boardword")
    hub = c.get("/v7").get_data(as_text=True)
    v5 = c.get("/v5").get_data(as_text=True)
    v6 = c.get("/v6").get_data(as_text=True)
    fails = 0
    for label, got, want in [
        ("hub renders", "Goals Dashboards" in hub, True),
        ("hub NOT limited", "hub password" in hub, False),
        ("/v5 board opens", "Sales Floor Scoreboard" in v5, True),
        ("/v6 board opens", "Billing Team KPI Board" in v6, True),
    ]:
        ok = got == want
        print("  %-24s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_wrong_word_opens_nothing():
    c = _client()
    r = _login(c, "/v7", "notthepassword")
    hub = c.get("/v7").get_data(as_text=True)
    fails = 0
    for label, got, want in [
        ("rejected with 401", r.status_code, 401),
        ("hub NOT rendered", "Goals Dashboards" in hub, False),
    ]:
        ok = got == want
        print("  %-24s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_throttle_covers_the_hub():
    """A short hub word is only as safe as the guess rate, so the throttle has to
    apply here too -- not just on /v5 where it was originally written."""
    appmod._v5_pw_tries.clear()
    c = _client()
    last = None
    for _ in range(appmod._V5_PW_MAX_TRIES + 2):
        last = _login(c, "/v7", "wrong")
    body = last.get_data(as_text=True)
    ok = "Too many tries" in body
    print("  %-24s want %-8s got %-8s %s"
          % ("throttled after burst", True, ok, "OK" if ok else "<<< FAIL"))
    appmod._v5_pw_tries.clear()
    return 0 if ok else 1


def run():
    total = 0
    for title, fn in [
        ("hub word opens ONLY the hub", case_hub_word_opens_only_the_hub),
        ("board word opens everything", case_board_word_opens_everything),
        ("wrong word opens nothing", case_wrong_word_opens_nothing),
        ("throttle covers the hub", case_throttle_covers_the_hub),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
