"""Call and text search page + recording player (2026-10-30).

Danny: "we have an option to view call history and call recordings via search
on the original version of the call tracker, can we add a super minimal same
functionality matching UI of the surgical coordinator, billing, sales and teams
kpi pages that will also link recordings where available and sms as before".

  /search                 the page; same door as /api/call-history/search
  /api/recording/<id>     a RingEX recording streamed with the app's token
                          (the old "Play" link opened RingCentral's 401)

Reads nothing from the network: RingCentral is replaced with a fake.
Run: ./.venv/bin/python -B checks_call_search.py   (tally LAST)
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


class FakeUp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.closed = False

    def iter_content(self, n):
        yield self._body

    def close(self):
        self.closed = True


calls = []


def fake_get(url, headers=None, stream=False, timeout=None):
    calls.append({"url": url, "headers": dict(headers or {})})
    if url.endswith("/recording/1234567/content"):
        rng = (headers or {}).get("Range")
        if rng:
            return FakeUp(206, b"ID3part", {"Content-Type": "audio/mpeg", "Content-Length": "7",
                                            "Content-Range": "bytes 0-6/53901"})
        return FakeUp(200, b"ID3whole", {"Content-Type": "audio/mpeg", "Content-Length": "8"})
    return FakeUp(404)


saved = (A.GOOGLE_CLIENT_ID, A.requests.get, A._ringcx._rc_headers, type(A._ringcx).configured)
try:
    A.GOOGLE_CLIENT_ID = "x"
    A.requests.get = fake_get
    A._ringcx._rc_headers = lambda: {"Authorization": "Bearer T"}
    type(A._ringcx).configured = property(lambda self: True)

    anon = A.app.test_client()
    r = anon.get("/search")
    ck("page: signed out goes to sign-in", r.status_code == 302 and "/login" in r.headers.get("Location", ""),
       (r.status_code, r.headers.get("Location")))
    r = anon.get("/api/recording/1234567")
    ck("recording: signed out is 401", r.status_code == 401, r.status_code)
    ck("recording: signed out never reaches RingCentral", not calls, calls)

    c = A.app.test_client()
    with c.session_transaction() as s:
        s["v5_pw"] = True                       # the floor word: GET only, like the old search
    r = c.get("/search?q=" + "%3Cscript%3Ealert(1)%3C%2Fscript%3E")
    body = r.get_data(as_text=True)
    ck("page: floor word opens it", r.status_code == 200, r.status_code)
    ck("page: board design (shared style tokens)", "--hair" in body and "Call &amp; Text Search" in body)
    ck("page: query is escaped into the box", "<script>alert(1)" not in body and "&lt;script&gt;" in body)
    ck("page: reads the original search API", "/api/call-history/search" in body)

    r = c.get("/api/recording/abc")
    ck("recording: non-numeric id refused", r.status_code in (400, 404), r.status_code)
    r = c.get("/api/recording/12")
    ck("recording: too-short id refused", r.status_code == 400, r.status_code)
    ck("recording: refused ids never reach RingCentral", not calls, calls)

    r = c.get("/api/recording/1234567")
    ck("recording: whole file streams", r.status_code == 200 and r.get_data() == b"ID3whole", (r.status_code, r.get_data()))
    ck("recording: audio content type", r.headers.get("Content-Type") == "audio/mpeg", r.headers.get("Content-Type"))
    ck("recording: player may seek", r.headers.get("Accept-Ranges") == "bytes")
    ck("recording: fixed media host, app token",
       calls and calls[-1]["url"] == "https://media.ringcentral.com/restapi/v1.0/account/~/recording/1234567/content"
       and calls[-1]["headers"].get("Authorization") == "Bearer T", calls[-1:])

    r = c.get("/api/recording/1234567", headers={"Range": "bytes=0-6"})
    ck("recording: range passes through as 206", r.status_code == 206 and r.get_data() == b"ID3part", r.status_code)
    ck("recording: content-range returned", r.headers.get("Content-Range") == "bytes 0-6/53901", r.headers.get("Content-Range"))
    ck("recording: range sent upstream", calls[-1]["headers"].get("Range") == "bytes=0-6", calls[-1])

    for bad in ("bytes=0-6, 10-20", "items=0-6"):
        r = c.get("/api/recording/1234567", headers={"Range": bad})
        ck("recording: odd range %r not forwarded" % bad, "Range" not in calls[-1]["headers"], calls[-1])

    r = c.get("/api/recording/7654321")
    ck("recording: missing upstream is 404", r.status_code == 404, r.status_code)
finally:
    (A.GOOGLE_CLIENT_ID, A.requests.get, A._ringcx._rc_headers, cfg) = saved
    type(A._ringcx).configured = cfg

for t in ("teams.html", "scoreboard_v5.html", "scoreboard_v6.html"):
    src = open(os.path.join(HERE, "templates", t)).read()
    ck("link on %s" % t, 'href="/search"' in src and ".srch" in (src + open(os.path.join(HERE, "templates", "_board_style.html")).read()))

print("%d passed, %d failed" % (passed, len(errors)))
for e in errors:
    print("  FAIL", e)
sys.exit(1 if errors else 0)
