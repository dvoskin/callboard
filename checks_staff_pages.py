"""Staff pages behind a word password: /team/<slug>/ (2026-10-26).

The page and its password live on the Render disk, never in this public repo. The lock
must hold (no page text before the word), the word must open it, every /team/ response
must say noindex, an unknown slug must look like any other 404, a submission must land
on disk exactly once, results need a real Google sign-in, and uploads need the machine key.
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


SLUG = "quiz-abc123"
SECRET_TEXT = "Which line is the right one"
saved = (dict(S._cfg), A.OVERDUE_API_KEY)
tmp = tempfile.mkdtemp()
try:
    S._cfg["root"] = Path(tmp) / "staff_pages"
    A.OVERDUE_API_KEY = "k-test"
    S._fails.clear()
    c = A.app.test_client()

    # nothing uploaded: a plain 404, and the upload needs the machine key
    r = c.get("/team/%s/" % SLUG)
    ck("unknown slug is a 404", r.status_code == 404, r.status_code)
    page = ('<form method="POST" action="/team/%s/submit"><p>%s</p>'
            '<input name="name"><input name="q1"><textarea name="story1"></textarea></form>' % (SLUG, SECRET_TEXT))
    body = {"page_html": page, "thanks_html": "<p>Thanks</p>", "password": "NewRep"}
    r = c.put("/api/staff-pages/%s" % SLUG, json=body)
    ck("upload without the key is refused", r.status_code == 401, r.status_code)
    r = c.put("/api/staff-pages/%s" % SLUG, json=body, headers={"X-API-Key": "nope"})
    ck("upload with a wrong key is refused", r.status_code == 401, r.status_code)
    r = c.put("/api/staff-pages/../etc", json=body, headers={"X-API-Key": "k-test"})
    ck("a path in the slug is refused", r.status_code in (400, 404), r.status_code)
    r = c.put("/api/staff-pages/%s" % SLUG, json=body, headers={"X-API-Key": "k-test"})
    ck("upload with the key works", r.status_code == 200 and r.get_json()["ok"], r.get_json())
    cfg = json.loads((Path(tmp) / "staff_pages" / SLUG / "config.json").read_text())
    ck("only a hash of the password is stored", "newrep" not in json.dumps(cfg).lower() and len(cfg["hash"]) == 64, cfg)

    # locked: no page text, noindex on every response
    r = c.get("/team/%s/" % SLUG)
    txt = r.get_data(as_text=True)
    ck("locked page shows the password box", r.status_code == 200 and 'name="password"' in txt, r.status_code)
    ck("locked page has none of the page text", SECRET_TEXT not in txt)
    for path in ("/team/%s/" % SLUG, "/team/%s/thanks" % SLUG, "/team/nope-404x/"):
        h = c.get(path).headers
        ck("noindex on %s" % path, "noindex" in h.get("X-Robots-Tag", ""), dict(h))
        ck("no-store on %s" % path, h.get("Cache-Control") == "no-store", h.get("Cache-Control"))
        ck("no-referrer on %s" % path, h.get("Referrer-Policy") == "no-referrer", h.get("Referrer-Policy"))

    # a submit before unlocking is not stored
    r = c.post("/team/%s/submit" % SLUG, data={"name": "Sneak", "q1": "A"})
    ck("submit while locked goes back to the lock", r.status_code == 303 and r.headers["Location"].endswith("/team/%s/" % SLUG), r.headers.get("Location"))
    ck("nothing stored from a locked submit", not (Path(tmp) / "staff_pages" / SLUG / "submissions").exists())

    # wrong word, then the right one (case and spaces do not matter)
    r = c.post("/team/%s/" % SLUG, data={"password": "wrong"})
    ck("wrong password is refused", r.status_code == 401 and SECRET_TEXT not in r.get_data(as_text=True), r.status_code)
    r = c.post("/team/%s/" % SLUG, data={"password": "  newREP "})
    ck("right password unlocks", r.status_code == 303, r.status_code)
    r = c.get("/team/%s/" % SLUG)
    ck("unlocked page shows the test", SECRET_TEXT in r.get_data(as_text=True))

    # one submission, stored on disk; a second is not
    r = c.post("/team/%s/submit" % SLUG, data={"name": "Ana Lee", "q1": "C", "story1": "Line 1\nLine 2", "company": ""})
    ck("submit goes to thanks", r.status_code == 303 and r.headers["Location"].endswith("/thanks"), r.headers.get("Location"))
    subs = list((Path(tmp) / "staff_pages" / SLUG / "submissions").glob("*.json"))
    ck("one submission on disk", len(subs) == 1, len(subs))
    rec = json.loads(subs[0].read_text())
    ck("answers stored as typed", rec["fields"] == {"name": "Ana Lee", "q1": "C", "story1": "Line 1\nLine 2"}, rec["fields"])
    c.post("/team/%s/submit" % SLUG, data={"name": "Ana Lee", "q1": "D"})
    ck("second submit from the same browser is not stored",
       len(list((Path(tmp) / "staff_pages" / SLUG / "submissions").glob("*.json"))) == 1)
    r = c.get("/team/%s/" % SLUG)
    ck("after submitting, the page goes to thanks", r.status_code == 303, r.status_code)

    # honeypot: looks accepted, stores nothing
    c2 = A.app.test_client()
    c2.post("/team/%s/" % SLUG, data={"password": "newrep"})
    c2.post("/team/%s/submit" % SLUG, data={"name": "Bot", "company": "x"})
    ck("honeypot submit not stored", len(list((Path(tmp) / "staff_pages" / SLUG / "submissions").glob("*.json"))) == 1)

    # results: a real Google account only; machine key for JSON
    c3 = A.app.test_client()
    r = c3.get("/team/%s/results" % SLUG)
    ck("results need a sign-in", r.status_code == 302 and "/login" in r.headers["Location"], r.status_code)
    with c3.session_transaction() as s:
        s["user"] = {"email": "admin@goalsplasticsurgery.com"}
    r = c3.get("/team/%s/results" % SLUG)
    ck("the shared admin word does not open results", r.status_code == 302, r.status_code)
    with c3.session_transaction() as s:
        s["user"] = {"email": "manager@goalsplasticsurgery.com"}
    r = c3.get("/team/%s/results" % SLUG)
    t = r.get_data(as_text=True)
    ck("a staff Google account sees results", r.status_code == 200 and "Ana Lee" in t and "Line 1<br>Line 2" in t, r.status_code)
    with c3.session_transaction() as s:
        s["user"] = {"email": "someone@gmail.com"}
    ck("an outside account does not", c3.get("/team/%s/results" % SLUG).status_code == 302)
    ck("JSON results need the key", c.get("/api/staff-pages/%s/submissions" % SLUG).status_code == 401)
    r = c.get("/api/staff-pages/%s/submissions" % SLUG, headers={"X-API-Key": "k-test"})
    ck("JSON results with the key", r.status_code == 200 and len(r.get_json()["submissions"]) == 1, r.status_code)

    # throttle: 8 wrong tries, then 429 even for the right word
    S._fails.clear()
    c4 = A.app.test_client()
    for _ in range(8):
        c4.post("/team/%s/" % SLUG, data={"password": "guess"})
    r = c4.post("/team/%s/" % SLUG, data={"password": "newrep"})
    ck("throttled after 8 wrong tries", r.status_code == 429, r.status_code)
finally:
    S._cfg.clear()
    S._cfg.update(saved[0])
    A.OVERDUE_API_KEY = saved[1]
    S._fails.clear()

for e in errors:
    print("FAIL:", e)
print("%d passed" % passed)
print("%d failed" % len(errors))
sys.exit(1 if errors else 0)
