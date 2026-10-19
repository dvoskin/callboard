"""Mint the Books-capable refresh token the catalog sync needs. Run once, locally.

The CRM token reads estimates and invoices and nothing under Settings --
and Items live under ZohoBooks.settings.*. GET /books/v3/items with it answers 401 code 57, so
catalog_sync can plan from quotes but cannot write an item until a token
with the settings scopes exists.

  ./.venv/bin/python setup_books_token.py

reads ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_ACCOUNTS_URL from .env,
prints the consent URL, waits for the redirect URL you land on
(http://localhost/?code=...), exchanges it, and writes the refresh token to
.books_token (git-ignored) -- never to stdout, never into tracked source.
Then set it on Render as ZOHO_BOOKS_REFRESH_TOKEN (with ZOHO_BOOKS_ORG_ID
773501182) and redeploy; /api/v5/diag books_probe says whether it took.
"""
import os
import sys
import urllib.parse
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# One Books token serves the whole app, so it must keep every read the
# boards already make. Retainer invoices are covered by no narrow scope
# (see the /api/v5/diag probe), so a token minted with settings + estimates
# + invoices + payments would have fixed the catalog and blanked the
# retainers column. fullaccess is what the estimator's own app holds.
SCOPES = "ZohoBooks.fullaccess.all"
OUT = HERE / ".books_token"


def main():
    cid, sec = os.environ.get("ZOHO_CLIENT_ID"), os.environ.get("ZOHO_CLIENT_SECRET")
    acc = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com")
    if not (cid and sec):
        sys.exit("ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET missing from .env")
    url = acc + "/oauth/v2/auth?" + urllib.parse.urlencode({
        "scope": SCOPES, "client_id": cid, "response_type": "code", "access_type": "offline",
        "prompt": "consent", "redirect_uri": "http://localhost"})
    print("\nOpen this in a browser signed in to the Zoho org that owns Books org 773501182:\n\n  %s\n" % url)
    print("Approve. The browser lands on http://localhost/?code=... (a blank or error page is fine).")
    landed = input("Paste that whole redirect URL here: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(landed).query).get("code", [""])[0] or landed
    resp = requests.post(acc + "/oauth/v2/token", data={
        "code": code, "client_id": cid, "client_secret": sec, "redirect_uri": "http://localhost",
        "grant_type": "authorization_code"}, timeout=20)
    data = resp.json()
    if "refresh_token" not in data:
        sys.exit("Exchange failed: %s" % {k: v for k, v in data.items() if k != "access_token"})
    OUT.write_text(data["refresh_token"] + "\n")
    os.chmod(OUT, 0o600)
    # Prove it before anyone pastes it anywhere: one items call.
    r = requests.get("https://www.zohoapis.com/books/v3/items",
                     headers={"Authorization": "Zoho-oauthtoken " + data["access_token"]},
                     params={"organization_id": os.environ.get("ZOHO_BOOKS_ORG_ID", "773501182"), "per_page": 1},
                     timeout=20)
    print("\nScopes granted: %s" % data.get("scope"))
    print("GET /items -> HTTP %d %s" % (r.status_code, "(items readable)" if r.ok else r.text[:120]))
    r2 = requests.get("https://www.zohoapis.com/books/v3/retainerinvoices",
                      headers={"Authorization": "Zoho-oauthtoken " + data["access_token"]},
                      params={"organization_id": os.environ.get("ZOHO_BOOKS_ORG_ID", "773501182"), "per_page": 1},
                      timeout=20)
    print("GET /retainerinvoices -> HTTP %d %s" % (r2.status_code, "(the boards keep their retainers)" if r2.ok else r2.text[:120]))
    print("\nRefresh token written to %s (not printed). Set it on Render as ZOHO_BOOKS_REFRESH_TOKEN." % OUT)


if __name__ == "__main__":
    main()
