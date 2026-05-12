"""
Run this once to exchange an authorization code for a refresh token.

Steps:
  1. Go to https://api-console.zoho.com
  2. Create a "Server-based Application"
  3. Set Redirect URI to: http://localhost
  4. Note your Client ID and Client Secret
  5. Visit the URL this script prints, approve access, copy the `code=` param from the redirect URL
  6. Paste the code when prompted — your refresh token will be printed
"""

import sys
import urllib.parse
import requests

SCOPES = "ZohoCRM.modules.ALL,ZohoCRM.settings.modules.ALL,ZohoCRM.coql.READ"

def main():
    client_id     = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    accounts_url  = input("Accounts URL [https://accounts.zoho.com]: ").strip() or "https://accounts.zoho.com"

    auth_url = (
        f"{accounts_url}/oauth/v2/auth?"
        + urllib.parse.urlencode({
            "scope": SCOPES,
            "client_id": client_id,
            "response_type": "code",
            "access_type": "offline",
            "redirect_uri": "http://localhost",
        })
    )
    print(f"\nOpen this URL in your browser:\n\n  {auth_url}\n")
    print("After approving, you'll be redirected to http://localhost?code=XXXX&...")
    code = input("Paste the `code` value here: ").strip()

    resp = requests.post(
        f"{accounts_url}/oauth/v2/token",
        params={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": "http://localhost",
            "grant_type": "authorization_code",
        },
    )
    data = resp.json()
    if "refresh_token" not in data:
        print(f"\nError: {data}")
        sys.exit(1)

    print(f"\nSuccess! Add these to your .env file:\n")
    print(f"ZOHO_CLIENT_ID={client_id}")
    print(f"ZOHO_CLIENT_SECRET={client_secret}")
    print(f"ZOHO_REFRESH_TOKEN={data['refresh_token']}")
    if accounts_url != "https://accounts.zoho.com":
        print(f"ZOHO_ACCOUNTS_URL={accounts_url}")

if __name__ == "__main__":
    main()
