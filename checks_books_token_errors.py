"""A token refresh failure must name its cause, and never leak the credential.

"Books token endpoint returned HTTP 400" is true and useless: invalid_code
means the value is not a refresh token, invalid_client means it belongs to a
different Zoho app, and those send you to opposite ends of the console. Zoho
says which in the response body; the board was throwing it away.

The reason it was thrown away is the second half of this. The credentials used
to ride in the QUERY STRING, so any error carrying the request URL carried the
client secret and refresh token with it -- and these errors reach
meta["errors"] in /api/v5/report, which every /v5/board?k= link holder can
read. They go in the form body now, which is what makes it safe to report the
error at all.

Run with no arguments. Reads nothing from the network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import books_client  # noqa: E402

SECRET = "super-secret-client-value"
TOKEN = "1000.deadbeef.cafebabe"


class _Resp:
    def __init__(self, status, payload, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _attempt(payload, status=400):
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["params"] = kw.get("params")
        seen["data"] = kw.get("data")
        return _Resp(status, payload)

    c = books_client.BooksClient()
    c.client_id, c.client_secret, c.refresh_token = "cid", SECRET, TOKEN
    c.org_id = "1"
    c._access_token = None
    c._token_expiry = None
    real = books_client.requests.post
    books_client.requests.post = fake_post
    try:
        c._get_access_token()
        return None, seen
    except Exception as e:  # noqa: BLE001
        return str(e), seen


def run():
    fails = 0
    cases = []

    msg, seen = _attempt({"error": "invalid_code"})
    cases += [
        ("invalid_code is named", "invalid_code" in (msg or ""), True),
        ("...with what it means", "not a valid refresh token" in (msg or ""), True),
        ("credentials NOT in the url", SECRET in (seen.get("url") or ""), False),
        ("...nor in query params", SECRET in str(seen.get("params")), False),
        ("...they are in the body", SECRET in str(seen.get("data")), True),
        ("the message leaks neither", SECRET in (msg or "") or TOKEN in (msg or ""), False),
    ]

    msg2, _ = _attempt({"error": "invalid_client"})
    cases += [
        ("invalid_client is named", "invalid_client" in (msg2 or ""), True),
        ("...points at the app", "different Zoho app" in (msg2 or ""), True),
    ]

    # A body that is not JSON, or carries no error field, must still fail
    # cleanly with the status code rather than raising something else.
    msg3, _ = _attempt(None, status=502)
    msg4, _ = _attempt({"nope": 1}, status=400)
    cases += [
        ("non-JSON body still reports", "502" in (msg3 or ""), True),
        ("...and does not crash", msg3 is not None, True),
        ("no error field still reports", "400" in (msg4 or ""), True),
    ]

    for label, got, want in cases:
        ok = got == want
        print("  %-32s want %-8s got %-8s %s" % (label, want, got, "OK" if ok else "<<< FAIL"))
        fails += 0 if ok else 1
    print("\n%d mismatched" % fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
