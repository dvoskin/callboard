"""The hub password must open every DASHBOARD, and no WRITE.

Danny asked for the hub to cross-authenticate the boards, so the separation is
no longer hub-vs-boards. It is READ vs WRITE: a word that gets handed round the
floor opens every dashboard, and still cannot text a patient, resolve a call or
edit a note -- those endpoints share the same decorator, and a shared word
should not reach them.

This drives the real Flask app through real sessions and asserts what the word
actually opens, in both directions -- "I only added it in one place" is exactly
the kind of claim that stops being true six months later.

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

# A marker only the real hub carries. "Goals Dashboards" used to serve, until
# the login page was given the destination's name as its title -- at which
# point a wrong password rendered a page containing the very string that was
# supposed to prove the hub had NOT rendered.
HUB_MARKER = 'href="/customer-service"' 


def _client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _login(c, where, word):
    return c.post(where, data={"password": word}, follow_redirects=False)


def case_hub_word_opens_every_dashboard():
    """The whole point of cross-authentication: one word, every board."""
    c = _client()
    _login(c, "/v7", "hubword")
    hub = c.get("/v7").get_data(as_text=True)
    v3 = c.get("/v3").get_data(as_text=True)
    v5 = c.get("/v5").get_data(as_text=True)
    v6 = c.get("/v6").get_data(as_text=True)
    fails = 0
    for label, got, want in [
        ("hub renders", HUB_MARKER in hub, True),
        ("no second-door note", "hub password" in hub, False),
        ("/v3 tracker opens", c.get("/v3").status_code, 200),
        ("/v5 talk time opens", "Sales Floor Scoreboard" in v5 or "id=\"out\"" in v5, True),
        ("/v6 board opens", "Team KPI Board" in v6, True),
        ("v6 API authorised", c.get("/api/v6/report").status_code != 401, True),
    ]:
        ok = got == want
        print("  %-24s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    return fails


def case_word_cannot_write():
    """A word handed round the floor must not be able to act on a patient.

    Same decorator guards the dashboards and the SMS sender; the split is the
    HTTP method, so this asserts the method split rather than a list of paths.
    """
    c = _client()
    _login(c, "/v7", "hubword")
    fails = 0
    for label, got, want in [
        ("POST resolve a call",
         c.post("/api/scheduled-call/abc123/resolve", json={}).status_code, 401),
        ("POST save interactions",
         c.post("/api/interactions/save", json={}).status_code, 401),
        ("POST analyze", c.post("/api/interactions/analyze", json={}).status_code, 401),
        ("GET still allowed", c.get("/api/v6/report").status_code != 401, True),
    ]:
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
        ("hub renders", HUB_MARKER in hub, True),
        ("hub NOT limited", "hub password" in hub, False),
        ("/v5 board opens", "Sales Floor Scoreboard" in v5, True),
        ("/v6 board opens", "Team KPI Board" in v6, True),
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
        ("hub NOT rendered", HUB_MARKER in hub, False),
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


def case_hub_login_page_names_the_hub():
    """The hub's login page was the SCOREBOARD's, title and all.

    /v7 shares v5_password.html, which said "Sales Floor Scoreboard" -- so
    someone following the hub link, typing the hub password and getting nowhere
    could not tell whether they were even at the right door. And when the hub
    password is not configured on an instance, that is indistinguishable from
    typing it wrong, by the one person who could check the setting.
    """
    import importlib
    fails = 0

    os.environ["V7_PASSWORDS"] = ""
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    pg = appmod.app.test_client().get("/v7").get_data(as_text=True)
    for label, got, want in [
        ("titled for the hub", "Goals Dashboards" in pg, True),
        ("not the scoreboard", "Sales Floor Scoreboard" in pg, False),
        ("missing hub password is named", "No hub password is set" in pg, True),
    ]:
        ok = got == want
        print("  %-24s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1

    os.environ["V7_PASSWORDS"] = "hubword"
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    pg2 = appmod.app.test_client().get("/v7").get_data(as_text=True)
    ok = "No hub password is set" not in pg2
    print("  %-24s want %-8s got %-8s %s"
          % ("...and hidden when set", True, ok, "OK" if ok else "<<< FAIL"))
    return fails + (0 if ok else 1)


def run():
    total = 0
    for title, fn in [
        ("hub word opens every dashboard", case_hub_word_opens_every_dashboard),
        ("word cannot WRITE", case_word_cannot_write),
        ("board word opens everything", case_board_word_opens_everything),
        ("wrong word opens nothing", case_wrong_word_opens_nothing),
        ("throttle covers the hub", case_throttle_covers_the_hub),
        ("hub login page names the hub", case_hub_login_page_names_the_hub),
    ]:
        print("\n== %s" % title)
        total += fn()
    print("\n%d mismatched" % total)
    return total


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
