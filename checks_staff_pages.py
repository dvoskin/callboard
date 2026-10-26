"""Staff pages behind the company Google sign-in: /team/<slug>/ (2026-10-26).

The page lives on the Render disk, never in this public repo. Signed out, no page text
(a redirect to /login); the shared admin word and outside accounts are refused without a
login loop; a company Google account opens it; every /team/ response says noindex; an
unknown slug looks like any other 404; one submission per Google account lands on disk;
results open for the page's managers only (trainees are signed in too); uploads need the
machine key.
Run: ./.venv/bin/python -B checks_staff_pages.py   (tally LAST)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app as A  # noqa: E402
import staff_pages as S  # noqa: E402

passed, errors = 0, []


def ck(label, cond, got=None):
    global passed
    if cond:
        passed += 1
    else:
        errors.append("%s -- got %r" % (label, got))


def sign_in(client, email):
    with client.session_transaction() as s:
        s["user"] = {"email": email, "name": email.split("@")[0]}


SLUG = "quiz-abc123"
SECRET_TEXT = "Which line is the right one"
MANAGER = "boss@goalsplasticsurgery.com"
saved = (dict(S._cfg), A.OVERDUE_API_KEY)
tmp = tempfile.mkdtemp()
subs_dir = Path(tmp) / "staff_pages" / SLUG / "submissions"
try:
    S._cfg["root"] = Path(tmp) / "staff_pages"
    A.OVERDUE_API_KEY = "k-test"
    c = A.app.test_client()

    # nothing uploaded: a plain 404; the upload needs the machine key and a company manager
    ck("unknown slug is a 404", c.get("/team/%s/" % SLUG).status_code == 404)
    page = ('<form method="POST" action="/team/%s/submit"><p>%s</p>'
            '<input name="name"><input name="q1"><textarea name="story1"></textarea></form>' % (SLUG, SECRET_TEXT))
    body = {"page_html": page, "thanks_html": "<p>Thanks</p>", "managers": [MANAGER]}
    ck("upload without the key is refused", c.put("/api/staff-pages/%s" % SLUG, json=body).status_code == 401)
    r = c.put("/api/staff-pages/%s" % SLUG, json=body, headers={"X-API-Key": "nope"})
    ck("upload with a wrong key is refused", r.status_code == 401, r.status_code)
    r = c.put("/api/staff-pages/%s" % SLUG, json=dict(body, managers=["x@gmail.com"]), headers={"X-API-Key": "k-test"})
    ck("an outside manager is refused", r.status_code == 400, r.status_code)
    r = c.put("/api/staff-pages/%s" % SLUG, json=body, headers={"X-API-Key": "k-test"})
    ck("upload with the key works", r.status_code == 200 and r.get_json()["ok"], r.get_json())

    # signed out: sent to Google sign-in, no page text, noindex everywhere
    r = c.get("/team/%s/" % SLUG)
    ck("signed out goes to /login with next", r.status_code == 302 and "/login?next=" in r.headers["Location"]
       and "team" in r.headers["Location"], r.headers.get("Location"))
    ck("signed out sees none of the page text", SECRET_TEXT not in r.get_data(as_text=True))
    for path in ("/team/%s/" % SLUG, "/team/%s/thanks" % SLUG, "/team/nope-404x/"):
        h = c.get(path).headers
        ck("noindex on %s" % path, "noindex" in h.get("X-Robots-Tag", ""), dict(h))
        ck("no-store on %s" % path, h.get("Cache-Control") == "no-store", h.get("Cache-Control"))
        ck("no-referrer on %s" % path, h.get("Referrer-Policy") == "no-referrer", h.get("Referrer-Policy"))
    r = c.post("/team/%s/submit" % SLUG, data={"name": "Sneak", "q1": "A"})
    ck("signed-out submit goes to /login", r.status_code == 302 and "/login" in r.headers["Location"], r.status_code)
    ck("nothing stored from a signed-out submit", not subs_dir.exists())

    # the shared admin word and an outside account: refused, and not bounced into a loop
    for who in ("admin@goalsplasticsurgery.com", "someone@gmail.com"):
        c2 = A.app.test_client()
        sign_in(c2, who)
        r = c2.get("/team/%s/" % SLUG)
        t = r.get_data(as_text=True)
        ck("%s refused with a switch-account page" % who, r.status_code == 403 and "/logout" in t
           and SECRET_TEXT not in t, r.status_code)

    # a trainee's own Google account: the page, one submission, then thanks
    t1 = A.app.test_client()
    sign_in(t1, "Ana.Lee@goalsplasticsurgery.com")
    ck("trainee sees the test", SECRET_TEXT in t1.get("/team/%s/" % SLUG).get_data(as_text=True))
    r = t1.post("/team/%s/submit" % SLUG, data={"name": "Ana Lee", "q1": "C", "story1": "Line 1\nLine 2", "company": ""})
    ck("submit goes to thanks", r.status_code == 303 and r.headers["Location"].endswith("/thanks"), r.headers.get("Location"))
    subs = list(subs_dir.glob("*.json"))
    ck("one submission on disk", len(subs) == 1, len(subs))
    rec = json.loads(subs[0].read_text())
    ck("answers stored as typed", rec["fields"] == {"name": "Ana Lee", "q1": "C", "story1": "Line 1\nLine 2"}, rec["fields"])
    ck("the Google account is recorded", rec["google_email"] == "ana.lee@goalsplasticsurgery.com", rec["google_email"])
    t1b = A.app.test_client()                        # same person, another browser
    sign_in(t1b, "ana.lee@goalsplasticsurgery.com")
    r = t1b.get("/team/%s/" % SLUG)
    ck("after submitting, the page goes to thanks (any browser)", r.status_code == 303 and r.headers["Location"].endswith("/thanks"), r.status_code)
    t1b.post("/team/%s/submit" % SLUG, data={"name": "Ana Lee", "q1": "D"})
    ck("a second submission from the same account is not stored", len(list(subs_dir.glob("*.json"))) == 1)
    ck("thanks opens for the trainee", t1b.get("/team/%s/thanks" % SLUG).status_code == 200)

    # honeypot: looks accepted, stores nothing
    t2 = A.app.test_client()
    sign_in(t2, "bo@goalsplasticsurgery.com")
    t2.post("/team/%s/submit" % SLUG, data={"name": "Bot", "company": "x"})
    ck("honeypot submit not stored", len(list(subs_dir.glob("*.json"))) == 1)

    # results: the page's managers only, not trainees; machine key for JSON
    r = t1.get("/team/%s/results" % SLUG)
    ck("a trainee cannot open results", r.status_code == 403 and "Line 1" not in r.get_data(as_text=True), r.status_code)
    m = A.app.test_client()
    ck("signed-out results go to /login", "/login" in m.get("/team/%s/results" % SLUG).headers.get("Location", ""))
    sign_in(m, MANAGER)
    r = m.get("/team/%s/results" % SLUG)
    t = r.get_data(as_text=True)
    ck("the manager sees results", r.status_code == 200 and "Ana Lee" in t and "Line 1<br>Line 2" in t
       and "ana.lee@goalsplasticsurgery.com" in t, r.status_code)
    ck("JSON results need the key", c.get("/api/staff-pages/%s/submissions" % SLUG).status_code == 401)
    r = c.get("/api/staff-pages/%s/submissions" % SLUG, headers={"X-API-Key": "k-test"})
    ck("JSON results with the key", r.status_code == 200 and len(r.get_json()["submissions"]) == 1, r.status_code)
finally:
    S._cfg.clear()
    S._cfg.update(saved[0])
    A.OVERDUE_API_KEY = saved[1]

for e in errors:
    print("FAIL:", e)
print("%d passed" % passed)
print("%d failed" % len(errors))
sys.exit(1 if errors else 0)
