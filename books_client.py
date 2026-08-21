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
        # The credentials ride in the query string, so an HTTPError raised here
        # carries them inside the URL -- and that string reaches meta["errors"]
        # in /api/v5/report, which every /v5/board?k= link holder can read.
        # Never let it escape: report the status code instead.
        if not resp.ok:
            raise RuntimeError(
                "Books token endpoint returned HTTP %d." % resp.status_code
            )
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Books token refresh failed: {data}")
        self._access_token = data["access_token"]
        self._token_expiry = datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 60)
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._get_access_token()}"}

    def _load_cache(self, date_start: str, date_end: str,
                    max_records: int, key: str = "estimates",
                    include_statuses: Optional[set] = None) -> list[dict]:
        """Return cached items (estimates or retainers) filtered to [date_start, date_end].

        When include_statuses is set, only items whose `status` is in the set
        are returned — mirrors the server-side status= filter so cache mode
        behaves identically.
        """
        try:
            blob = json.loads(self.cache_path.read_text())
        except Exception as e:
            log.warning("Failed to read cache %s: %s", self.cache_path, e)
            return []
        items = blob.get(key, []) if isinstance(blob, dict) else (blob or [])
        filt = [it for it in items
                if (it.get("date") or "") >= date_start and (it.get("date") or "") <= date_end]
        if include_statuses:
            filt = [it for it in filt if (it.get("status") or "") in include_statuses]
        filt.sort(key=lambda it: it.get("date") or "", reverse=True)
        log.info("Books CACHE: %d %s between %s and %s%s (file: %s)",
                 len(filt), key, date_start, date_end,
                 f" [status={'+'.join(sorted(include_statuses))}]" if include_statuses else "",
                 self.cache_path.name)
        return filt[:max_records]

    # An estimate stops reading "sent" the moment it moves on -- viewed by the
    # customer, accepted, declined, expired, or converted to an invoice. Filtering
    # status=="sent" therefore counts only the quotes that have gone NOWHERE, and
    # penalises a rep exactly when a quote succeeds.
    #
    # Measured against Books for 2026-08-20: 125 estimates, 1 draft, so 124 quotes
    # actually left the office -- the board showed 100. All 24 it dropped had been
    # invoiced. The error was not uniform, so the RANKING was wrong too: Adelita
    # Flowers read 6 against 10 sent, Alicia Mckenzie 2 against 5, while Adriana
    # Gentry (13, none converted) was untouched and stayed on top.
    #
    # For a scoreboard "sent" means "it left the office", so the test is the
    # complement: everything except the states that mean it never went out.
    _NEVER_SENT_ESTIMATE_STATUSES = frozenset({"draft", "pending_approval"})

    def list_sent_estimates(
        self,
        date_start: str,
        date_end: str,
        max_records: int = 2000,
    ) -> list[dict]:
        return self._list_documents(
            "estimates", date_start, date_end, max_records,
            exclude_statuses=set(self._NEVER_SENT_ESTIMATE_STATUSES))

    # Retainer statuses that still owe money — these are what the Follow Up
    # Tracker needs to chase. In Goals' workflow most retainers go straight
    # to `partially_paid` because the customer drops a deposit on send, so a
    # literal status="sent" filter would yield almost nothing. The set below
    # captures every "money still owed" state Books exposes.
    _UNPAID_INVOICE_STATUSES = frozenset(
        # `partially_paid` deliberately excluded — patient has dropped a deposit
        # so the deal is already in motion; the manager wants only retainers
        # that haven't seen any payment yet.
        {"sent", "viewed", "overdue", "unpaid"}
    )

    def list_sent_retainer_invoices(
        self,
        date_start: str,
        date_end: str,
        max_records: int = 500,
    ) -> list[dict]:
        """Retainer invoices in [date_start, date_end] that are still waiting
        for payment. Excludes `paid` (fully paid — no follow-up needed) and
        `draft` / `void` (never sent in the first place). Defensive: also
        drops rows whose balance has dropped to $0 even if Books status says
        partially_paid (status can lag behind the last payment).
        """
        rows = self._list_documents(
            "invoices", date_start, date_end, max_records,
            # Books status= is single-valued, so apply the whitelist client-side
            # (live API filter) and via the cache fallback's include_statuses.
            status=None,
            include_statuses=self._UNPAID_INVOICE_STATUSES,
        )
        # A balance > 0 means there's actually still money owed. Books occasionally
        # leaves an invoice at status=partially_paid after the final payment lands,
        # so check the number directly. Defensive parse: any non-numeric balance
        # keeps the row (better to show a row we can't classify than 500 out).
        def _still_owing(r):
            bal = r.get("balance")
            if bal is None or bal == "":
                return True
            try:
                return float(bal) > 0.01
            except (TypeError, ValueError):
                log.warning("Books retainers: invalid balance %r for invoice %s — keeping",
                            bal, r.get("invoice_number"))
                return True
        before = len(rows)
        rows = [r for r in rows if _still_owing(r)]
        if len(rows) != before:
            log.info("Books retainers: filtered %d rows with zero balance",
                     before - len(rows))
        return rows

    def list_paid_retainer_invoices(
        self,
        date_start: str,
        date_end: str,
        max_records: int = 500,
    ) -> list[dict]:
        """Retainer invoices in [date_start, date_end] that are fully PAID.

        This is the real "retainers paid" signal — an actual payment fact —
        as opposed to a CRM deal stage. In Goals' Books org, invoices ARE the
        retainer invoices. A row counts as paid when Books status is `paid`
        (server-side filter) or its balance has reached zero.
        """
        rows = self._list_documents(
            "invoices", date_start, date_end, max_records,
            status="paid",
            include_statuses={"paid"},  # also constrains the cache fallback
        )

        # Belt-and-suspenders: keep rows Books reports paid, plus any whose
        # balance is zero even if the status string lags behind.
        def _is_paid(r):
            if (r.get("status") or "").lower() == "paid":
                return True
            bal = r.get("balance")
            try:
                return bal not in (None, "") and float(bal) <= 0.01
            except (TypeError, ValueError):
                return False
        return [r for r in rows if _is_paid(r)]

    def list_retainer_payments(
        self,
        date_start: str,
        date_end: str,
        lookback_days: int = 180,
        max_records: int = 500,
    ) -> list[dict]:
        """Payments RECEIVED in [date_start, date_end], attributed to the
        salesperson on the invoice each one settles.

        This is NOT list_paid_retainer_invoices. That filters on the INVOICE
        date and asks "is it paid now", so a retainer raised on Monday and paid
        today never appears on today's board. A daily scoreboard wants the
        payment fact on the day the money arrived, which means filtering on the
        payment date and then looking up who owns the invoice.

        Payments carry no salesperson, so invoices over a wider window are
        fetched once and used as the lookup rather than one call per payment.
        """
        if not (self.client_id and self.client_secret and self.refresh_token and self.org_id):
            log.info("Books not configured — no retainer payments available")
            return []

        # Resolve the token OUTSIDE the try. _headers() refreshes the OAuth token,
        # and a refresh failure is an auth problem, not a transient network one --
        # swallowing it here returned [] and reported "0 retainers paid" with no
        # error at all, while the other two metrics correctly surfaced the same
        # failure. Let it propagate.
        headers = self._headers()

        raw, page, per_page = [], 1, 200
        while len(raw) < max_records:
            try:
                resp = requests.get(
                    f"{self.base_url}/customerpayments",
                    headers=headers,
                    params={"organization_id": self.org_id,
                            "date_start": date_start, "date_end": date_end,
                            "sort_column": "date", "page": page, "per_page": per_page},
                    timeout=20,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("Books customerpayments request failed: %s", e)
                break
            if resp.status_code == 204:
                break
            if not resp.ok:
                # Raise. Returning [] here made a missing scope look exactly like a
                # day with no payments, and the board would print a confident zero.
                detail = (resp.text or "")[:200]
                hint = (" The Books OAuth token is missing the "
                        "ZohoBooks.customerpayments.READ scope."
                        if resp.status_code in (401, 403) else "")
                log.warning("Books customerpayments %s: %s", resp.status_code, detail)
                raise RuntimeError("Books customerpayments returned HTTP %d.%s %s"
                                   % (resp.status_code, hint, detail))
            payload = resp.json() or {}
            rows = payload.get("customerpayments") or payload.get("customer_payments") or []
            raw.extend(rows)
            if len(rows) < per_page:
                break
            page += 1

        if not raw:
            log.info("Books: no payments received %s..%s", date_start, date_end)
            return []

        # Only now is the invoice lookup worth its ~10s and 2000 rows.
        from datetime import date as _date, timedelta as _td
        try:
            back = (_date.fromisoformat(date_start) - _td(days=lookback_days)).isoformat()
        except ValueError:
            back = date_start
        owner = {}
        try:
            for inv in self._list_documents("invoices", back, date_end, 2000):
                who = (inv.get("salesperson_name") or "").strip()
                if not who:
                    continue
                for k in (inv.get("invoice_number"), inv.get("invoice_id")):
                    if k:
                        owner[str(k)] = who
        except Exception as e:  # noqa: BLE001
            log.warning("Books: invoice lookup for payments failed: %s", e)

        out, unmatched = [], 0
        for r in raw:
            # The list shape varies by Books version: sometimes an `invoices`
            # array, sometimes a comma-joined `invoice_numbers` string. Accept
            # both rather than betting on one.
            keys = []
            for inv in (r.get("invoices") or []):
                keys += [str(inv.get("invoice_number") or ""), str(inv.get("invoice_id") or "")]
            for n in str(r.get("invoice_numbers") or "").split(","):
                if n.strip():
                    keys.append(n.strip())
            who = next((owner[k] for k in keys if k in owner), "")
            if not who:
                unmatched += 1
            r = dict(r)
            r["salesperson_name"] = who or "Unassigned"
            out.append(r)
        if unmatched:
            # Say it rather than letting the total quietly land in "Unassigned".
            log.info("Books payments: %d of %d could not be matched to an invoice "
                     "owner (invoice older than the %d-day lookback, or no salesperson set)",
                     unmatched, len(out), lookback_days)
        log.info("Books: %d payments received %s..%s", len(out), date_start, date_end)
        return out[:max_records]

    def _list_documents(
        self,
        doc_type: str,
        date_start: str,
        date_end: str,
        max_records: int,
        status: Optional[str] = None,
        exclude_statuses: Optional[set] = None,
        include_statuses: Optional[set] = None,
    ) -> list[dict]:
        """Shared list+paginate logic for estimates and invoices.

        - status: passes through to the Books API as ?status=X (server-side filter).
        - exclude_statuses: drop matching items client-side after fetch.
        - include_statuses: keep ONLY matching items client-side. Used by the
          cache fallback so the same whitelist applies whether we hit the live
          API or the local snapshot.
        """
        cache_key = "estimates" if doc_type == "estimates" else "retainers"
        self.last_source_was_cache = False
        # Cache-only mode (no creds configured)
        if not (self.client_id and self.client_secret and self.refresh_token and self.org_id):
            if self.cache_path.exists():
                self.last_source_was_cache = True
                return self._load_cache(date_start, date_end, max_records,
                                         key=cache_key, include_statuses=include_statuses)
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
                    return self._load_cache(date_start, date_end, max_records,
                                             key=cache_key, include_statuses=include_statuses)
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
            if include_statuses:
                batch = [b for b in batch if (b.get("status") or "") in include_statuses]
            results.extend(batch)
            if not data.get("page_context", {}).get("has_more_page"):
                break
            page += 1

        log.info("Books: %d %s between %s and %s",
                 len(results), doc_type, date_start, date_end)
        return results[:max_records]
