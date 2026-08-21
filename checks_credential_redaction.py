"""Regression: a Zoho credential must never reach a response or a log line.

Zoho takes its OAuth credentials as *query parameters*, so when the token
endpoint returns a non-200 the resulting HTTPError message is:

    400 Client Error: Bad Request for url:
    https://accounts.zoho.com/oauth/v2/token?refresh_token=1000.<70 chars>&client_secret=...

app.py's _v5_books put str(e)[:200] into meta["errors"], which /api/v5/report
returns -- so every holder of a /v5/board?k= link would have been served the
refresh token. The 200-char window is wide enough to contain all of it.

It never fired only because Zoho answers a *bad refresh token* with HTTP 200 and
{"error": "invalid_code"}, so the clean RuntimeError won the race. Any 400/401/429
from the token endpoint -- a rate limit, an outage -- would have published it.

Two guards, because there are two ways to lose it:
  1. the clients must not raise a URL-bearing exception at all (root fix)
  2. _redact must strip credentials from anything else that gets stringified
"""
import sys, types

sys.path.insert(0, __file__.rsplit('/', 1)[0])

SECRET = "1000." + "a1b2c3d4e5" * 6 + "f7g8h9"      # 70 chars, like the real one
CLIENT_SECRET = "SUPERSECRETVALUE1234567890"
LEAK_URL = (
    "400 Client Error: Bad Request for url: "
    "https://accounts.zoho.com/oauth/v2/token"
    f"?refresh_token={SECRET}&client_id=1000.CLIENTID&"
    f"client_secret={CLIENT_SECRET}&grant_type=refresh_token"
)

failed = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + detail) if not cond else ""))
    if not cond:
        failed.append(name)


# ---- 1. the clients must not produce a URL-bearing exception -----------------
def _non_200(mod, attr_cls, label):
    class R:
        status_code, ok = 429, False
        text = "rate limited"
        url = LEAK_URL
        def json(self): return {}
        def raise_for_status(self): raise Exception(LEAK_URL)
    mod.requests = types.SimpleNamespace(post=lambda *a, **k: R(), get=lambda *a, **k: R())
    c = attr_cls.__new__(attr_cls)
    c.client_id, c.client_secret = "1000.CLIENTID", CLIENT_SECRET
    c.refresh_token = SECRET
    c.accounts_url = "https://accounts.zoho.com"
    c._access_token = None
    c._token_expiry = None
    try:
        c._get_access_token()
        check(f"{label}: non-200 raises", False, "no exception raised")
    except Exception as e:
        msg = str(e)
        check(f"{label}: refresh token absent from exception", SECRET not in msg, msg[:160])
        check(f"{label}: client secret absent from exception", CLIENT_SECRET not in msg, msg[:160])
        check(f"{label}: status code is reported", "429" in msg, msg[:160])


import books_client as bc
_non_200(bc, bc.BooksClient, "books")

import zoho_client as zc
_non_200(zc, zc.ZohoClient, "crm")

# ---- 2. _redact strips credentials from anything else ------------------------
import re
_C = re.compile(
    r"((?:refresh_token|client_secret|client_id|access_token|code|authtoken)=)[^&\s\"']+",
    re.I,
)
def _redact(text):
    return _C.sub(r"\1<redacted>", str(text))

out = _redact(LEAK_URL)
check("redact: refresh token removed", SECRET not in out, out[:160])
check("redact: client secret removed", CLIENT_SECRET not in out, out[:160])
check("redact: non-secret text survives", "grant_type=refresh_token" in out, out[:160])
check("redact: truncation window is still clean", SECRET not in _redact(LEAK_URL)[:200])

# the helper app.py actually ships must behave the same as the one asserted here
import io
src = io.open("app.py", encoding="utf-8").read()
check("app.py defines _redact", "def _redact(" in src)
check("app.py redacts the books error detail",
      'meta["errors"].append({"metric": label, "detail": _redact(e)[:200]})' in src)
check("app.py redacts the books log line",
      'log.warning("v5 books %s failed: %s", label, _redact(e))' in src)

print(f"\n{len(failed)} failed")
sys.exit(1 if failed else 0)
