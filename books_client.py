"""Minimal Zoho Books client for fetching sent estimates (a.k.a. "quotes").

Books and CRM share the same Zoho OAuth app but require different scopes.
Configure with a Books-specific refresh token + org id, or with a combined-scope
refresh token in ZOHO_REFRESH_TOKEN (must include ZohoBooks.estimates.READ).

Required env:
  ZOHO_BOOKS_ORG_ID      — Books organization id (e.g. 773501182 for Goals)
  ZOHO_BOOKS_REFRESH_TOKEN  — optional; falls back to ZOHO_REFRESH_TOKEN
  ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET — shared with the CRM client
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_CACHE = Path(__file__).parent / "data" / "quotes_cache.json"


class BooksClient:
    def __init__(self):
        self.client_id = os.getenv("ZOHO_CLIENT_ID")
        self.client_secret = os.getenv("ZOHO_CLIENT_SECRET")
        self.refresh_token = (
            os.getenv("ZOHO_BOOKS_REFRESH_TOKEN") or os.getenv("ZOHO_REFRESH_TOKEN")
        )
        self.org_id = os.getenv("ZOHO_BOOKS_ORG_ID", "")
        self.accounts_url = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com")
        self.base_url = os.getenv("ZOHO_BOOKS_BASE_URL", "https://www.zohoapis.com/books/v3")
        # Local prototyping fallback: when the live API can't be reached (missing
        # Books scope, no org id), serve from a cached estimate dump if present.
        self.cache_path = Path(os.getenv("ZOHO_BOOKS_CACHE_FILE", str(_DEFAULT_CACHE)))
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        # True iff the most recent list_sent_estimates() call served from cache.
        self.last_source_was_cache: bool = False

    @property
    def configured(self) -> bool:
        if self.client_id and self.client_secret and self.refresh_token and self.org_id:
            return True
        # Cache-only mode is also "configured" — lets the panel render in local dev.
        return self.cache_path.exists()

    def _get_access_token(self) -> str:
        if self._access_token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._access_token
        resp = requests.post(
            f"{self.accounts_url}/oauth/v2/token",
            params={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Books token refresh failed: {data}")
        self._access_token = data["access_token"]
        self._token_expiry = datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 60)
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._get_access_token()}"}

    def _load_cache(self, date_start: str, date_end: str,
                    max_records: int, key: str = "estimates") -> list[dict]:
        """Return cached items (estimates or retainers) filtered to [date_start, date_end]."""
        try:
            blob = json.loads(self.cache_path.read_text())
        except Exception as e:
            log.warning("Failed to read cache %s: %s", self.cache_path, e)
            return []
        items = blob.get(key, []) if isinstance(blob, dict) else (blob or [])
        filt = [it for it in items
                if (it.get("date") or "") >= date_start and (it.get("date") or "") <= date_end]
        filt.sort(key=lambda it: it.get("date") or "", reverse=True)
        log.info("Books CACHE: %d %s between %s and %s (file: %s)",
                 len(filt), key, date_start, date_end, self.cache_path.name)
        return filt[:max_records]

    def list_sent_estimates(
        self,
        date_start: str,
        date_end: str,
        max_records: int = 500,
    ) -> list[dict]:
        return self._list_documents("estimates", date_start, date_end, max_records,
                                     status="sent")

    def list_sent_retainer_invoices(
        self,
        date_start: str,
        date_end: str,
        max_records: int = 500,
    ) -> list[dict]:
        """Sent retainer invoices in [date_start, date_end].

        Books has no "retainer" status — any non-draft / non-void invoice in
        this org IS a retainer (Goals' invoicing workflow). The endpoint also
        supports status= filter so we don't pull drafts.
        """
        return self._list_documents("invoices", date_start, date_end, max_records,
                                     # Pull sent + partially_paid + paid + overdue + unpaid + viewed
                                     status=None, exclude_statuses={"draft", "void"})

    def _list_documents(
        self,
        doc_type: str,
        date_start: str,
        date_end: str,
        max_records: int,
        status: Optional[str] = None,
        exclude_statuses: Optional[set] = None,
    ) -> list[dict]:
        """Shared list+paginate logic for estimates and invoices."""
        cache_key = "estimates" if doc_type == "estimates" else "retainers"
        self.last_source_was_cache = False
        # Cache-only mode (no creds configured)
        if not (self.client_id and self.client_secret and self.refresh_token and self.org_id):
            if self.cache_path.exists():
                self.last_source_was_cache = True
                return self._load_cache(date_start, date_end, max_records, key=cache_key)
            raise RuntimeError("Books client not configured. Set ZOHO_BOOKS_ORG_ID and a Books refresh token, or drop a quotes_cache.json in data/.")

        results: list[dict] = []
        page = 1
        per_page = 200
        while len(results) < max_records:
            params = {
                "organization_id": self.org_id,
                "date_start": date_start,
                "date_end": date_end,
                "sort_column": "date",
                "page": page,
                "per_page": per_page,
            }
            if status:
                params["status"] = status
            resp = requests.get(
                f"{self.base_url}/{doc_type}",
                headers=self._headers(),
                params=params,
                timeout=20,
            )
            if resp.status_code == 204:
                break
            if resp.status_code in (401, 403):
                # Scope or auth problem. In local dev a cache file lets us still
                # render real data; in prod, surface the error so the panel can
                # tell the user to add the right Books scope.
                if self.cache_path.exists():
                    log.warning(
                        "Books live fetch %s — falling back to cache file %s",
                        resp.status_code, self.cache_path.name,
                    )
                    self.last_source_was_cache = True
                    return self._load_cache(date_start, date_end, max_records, key=cache_key)
                scope_hint = ("ZohoBooks.estimates.READ" if doc_type == "estimates"
                              else "ZohoBooks.invoices.READ")
                raise RuntimeError(
                    f"Books API auth error {resp.status_code} — the refresh token "
                    f"likely lacks {scope_hint} scope. ({resp.text[:120]})"
                )
            if not resp.ok:
                log.warning("Books list %s error %s: %s",
                            doc_type, resp.status_code, resp.text[:200])
                break
            data = resp.json() or {}
            batch = data.get(doc_type, []) or []
            if exclude_statuses:
                batch = [b for b in batch if b.get("status") not in exclude_statuses]
            results.extend(batch)
            if not data.get("page_context", {}).get("has_more_page"):
                break
            page += 1

        log.info("Books: %d %s between %s and %s",
                 len(results), doc_type, date_start, date_end)
        return results[:max_records]
