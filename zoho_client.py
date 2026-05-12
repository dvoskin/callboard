import os
import re
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

NEW_DEAL_WINDOW_MINUTES = 5
NEW_DEAL_LOOKBACK_HOURS = 48
# Timing thresholds for scheduled call classification:
#   < -EARLY_BEFORE_MIN  → "early"           (more than 5 min before scheduled)
#   -EARLY_BEFORE_MIN to +ON_TIME_AFTER_MIN  → "completed" on_time=True  (-5 to +10 min)
#   > +ON_TIME_AFTER_MIN → "completed" on_time=False  (late, > 10 min after)
EARLY_BEFORE_MIN     = int(os.getenv("EARLY_BEFORE_MIN", "5"))
ON_TIME_AFTER_MIN    = int(os.getenv("ON_TIME_AFTER_MIN", "10"))
# Legacy: kept only for compatibility with anything still reading SCHEDULED_TOLERANCE_MINUTES
SCHEDULED_CALL_TOLERANCE_MINUTES = ON_TIME_AFTER_MIN
# Max window (minutes) within which a call counts as fulfilling a scheduled slot.
SCHEDULED_CALL_MAX_MATCH_MINUTES = int(os.getenv("SCHEDULED_MAX_MATCH_MINUTES", "720"))  # 12 hours
# Local timezone offset from UTC (e.g. -6 for CST, -5 for CDT)
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "-6"))


def normalize_phone(phone: str) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


class ZohoClient:
    def __init__(self):
        self.client_id = os.getenv("ZOHO_CLIENT_ID")
        self.client_secret = os.getenv("ZOHO_CLIENT_SECRET")
        self.refresh_token = os.getenv("ZOHO_REFRESH_TOKEN")
        self.base_url = os.getenv("ZOHO_BASE_URL", "https://www.zohoapis.com")
        self.accounts_url = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com")
        self._access_token = None
        self._token_expiry = None

    # ------------------------------------------------------------------ auth

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
            raise RuntimeError(f"Token refresh failed: {data}")
        self._access_token = data["access_token"]
        self._token_expiry = datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 60)
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._get_access_token()}"}

    # --------------------------------------------------------------- deals

    def _today_bounds(self) -> tuple:
        """Returns (start_of_today, end_of_today) in UTC."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return today_start, today_end

    def _get_deals_by_stage(self, stage: str) -> list[dict]:
        import logging
        select_fields = (
            "id, Deal_Name, Phone, Created_Time, Stage, Contact_Name, Owner, "
            "DialAttempts, DialerStatus, Call_Scheduled_Date, Best_Contact_Time"
        )
        now = datetime.now(timezone.utc)
        today_start, today_end = self._today_bounds()
        # New Deals: rolling 48-hour lookback window
        nd_start = now - timedelta(hours=NEW_DEAL_LOOKBACK_HOURS)

        all_deals, offset = [], 0
        while True:
            query = (
                f"select {select_fields} from Deals "
                f"where Stage = '{stage}' and id is not null "
                f"order by Created_Time desc "
                f"limit 200 offset {offset}"
            )
            resp = requests.post(
                f"{self.base_url}/crm/v6/coql",
                headers=self._headers(),
                json={"select_query": query},
                timeout=30,
            )
            if resp.status_code == 204:
                break
            if not resp.ok:
                logging.getLogger(__name__).error(
                    "COQL deals error %s: %s", resp.status_code, resp.text[:300]
                )
                break
            data = resp.json()
            batch = data.get("data", [])

            done = False
            for deal in batch:
                created = self._parse_dt(deal.get("Created_Time"))
                # New Deal: keep last 48 hours; stop once we pass the window
                if stage == "New Deal":
                    if created and created < nd_start:
                        done = True
                        break
                    if created and created >= nd_start:
                        all_deals.append(deal)
                # Call Scheduled: keep only those with Call_Scheduled_Date today
                else:
                    sched = self._parse_dt(deal.get("Call_Scheduled_Date"))
                    if sched and today_start <= sched <= today_end:
                        all_deals.append(deal)
                    elif created and created < today_start:
                        done = True
                        break

            if done or not data.get("info", {}).get("more_records"):
                break
            offset += 200

        return all_deals

    # --------------------------------------------------------------- calls

    @staticmethod
    def _phone_from_subject(subject: str) -> Optional[str]:
        """Extract a normalized 10-digit phone from a Subject like 'Outbound Call to +16203919116'."""
        if not subject:
            return None
        # Find the last run of digits (strip country code if present)
        digits = re.sub(r"\D", "", subject)
        return digits[-10:] if len(digits) >= 10 else None

    def _call_window(self) -> tuple[str, str]:
        """Returns (start_str, end_str) covering the New Deal lookback window through end of today."""
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=NEW_DEAL_LOOKBACK_HOURS)
        _, today_end = self._today_bounds()
        return (
            window_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            today_end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        )

    def _fetch_calls_batch_by_subject(
        self, phones: list[str], start_str: str, end_str: str
    ) -> Optional[list[dict]]:
        """Query calls whose Subject contains one of the given phones.

        Returns None if COQL rejects the query (signals caller to use full-dump fallback).
        """
        conditions = " or ".join(f"Subject like '%{p}%'" for p in phones)
        query = (
            "select id, Subject, Call_Start_Time, "
            "Outgoing_call_disposition, Call_Duration_in_seconds, "
            "Call_Type, Owner, Created_Time "
            f"from Calls where Call_Start_Time between '{start_str}' and '{end_str}' "
            f"and ({conditions}) "
            "order by Call_Start_Time desc limit 200"
        )
        resp = requests.post(
            f"{self.base_url}/crm/v6/coql",
            headers=self._headers(),
            json={"select_query": query},
            timeout=20,
        )
        if resp.status_code == 204:
            return []
        if not resp.ok:
            return None  # signal: COQL rejected the query
        return resp.json().get("data", [])

    def _fetch_calls_full_dump(self, start_str: str, end_str: str) -> list[dict]:
        """Fallback: fetch all calls in window, capped at 1 000 records."""
        import logging
        log = logging.getLogger(__name__)
        all_calls, offset = [], 0
        while offset < 1000:
            query = (
                "select id, Subject, Call_Start_Time, "
                "Outgoing_call_disposition, Call_Duration_in_seconds, "
                "Call_Type, Owner, Created_Time "
                f"from Calls where Call_Start_Time between '{start_str}' and '{end_str}' "
                "and id is not null "
                f"order by Call_Start_Time desc limit 200 offset {offset}"
            )
            resp = requests.post(
                f"{self.base_url}/crm/v6/coql",
                headers=self._headers(),
                json={"select_query": query},
                timeout=20,
            )
            if resp.status_code == 204:
                break
            if not resp.ok:
                log.warning("Full-dump calls fetch failed %s: %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            all_calls.extend(c for c in data.get("data", []) if c.get("Call_Type") == "Outbound")
            if not data.get("info", {}).get("more_records"):
                break
            offset += 200
        if offset >= 1000:
            log.warning("Call full-dump capped at 1000 records — some matches may be missing")
        return all_calls

    def _fetch_all_calls_for_phones(self, phones: list[str]) -> dict[str, list[dict]]:
        """Returns a map of normalized last-10-digit phone → list of matching call records.

        Primary strategy: COQL batches of 5 phones using Subject like '%XXXXXXXXXX%'.
        Fallback: fetch all calls in window and match by Subject (capped at 1 000 records).
        """
        import logging
        log = logging.getLogger(__name__)

        phone_map: dict[str, list[dict]] = {}
        unique_phones: list[str] = []
        for p in phones:
            n = normalize_phone(p)
            if n and n not in phone_map:
                unique_phones.append(n)
                phone_map[n] = []

        if not unique_phones:
            return phone_map

        start_str, end_str = self._call_window()
        BATCH = 5
        used_fallback = False

        for i in range(0, len(unique_phones), BATCH):
            batch = unique_phones[i : i + BATCH]
            calls = self._fetch_calls_batch_by_subject(batch, start_str, end_str)
            if calls is None:
                # COQL rejected Subject like — switch to full dump for everything
                log.warning("Subject-like COQL failed; switching to full-dump fallback")
                all_calls = self._fetch_calls_full_dump(start_str, end_str)
                for call in all_calls:
                    phone = self._phone_from_subject(call.get("Subject", "") or "")
                    if phone and phone in phone_map:
                        phone_map[phone].append(call)
                used_fallback = True
                break
            for call in calls:
                phone = self._phone_from_subject(call.get("Subject", "") or "")
                if phone and phone in phone_map:
                    phone_map[phone].append(call)

        total = sum(len(v) for v in phone_map.values())
        strategy = "full-dump fallback" if used_fallback else f"Subject-like ({len(unique_phones)} phones, {-(-len(unique_phones)//BATCH)} batches)"
        log.info("  → %d calls matched via %s", total, strategy)
        return phone_map

    # --------------------------------------------------- classification

    def _parse_dt(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _caller_name(call: Optional[dict]) -> Optional[str]:
        if not call:
            return None
        owner = call.get("Owner")
        if isinstance(owner, dict):
            return owner.get("name")
        return owner if isinstance(owner, str) else None

    def _classify_new_deal(self, deal: dict, calls: list[dict]) -> dict:
        created = self._parse_dt(deal.get("Created_Time"))
        now = datetime.now(timezone.utc)
        elapsed_min = (now - created).total_seconds() / 60 if created else None

        # Calls with a disposition = actual dial attempts
        dialed = [c for c in calls if c.get("Outgoing_call_disposition")]
        dial_attempts = len(dialed)
        # Most recent dialed call drives the displayed disposition
        most_recent = max(dialed, key=lambda c: c.get("Call_Start_Time") or "") if dialed else None

        if dialed:
            first = min(dialed, key=lambda c: c.get("Call_Start_Time") or "")
            call_dt = self._parse_dt(first.get("Call_Start_Time"))
            minutes_to_call = (
                (call_dt - created).total_seconds() / 60 if call_dt and created else None
            )
            return {
                "status": "completed",
                "minutes_to_first_call": round(minutes_to_call, 1) if minutes_to_call is not None else None,
                "on_time": (minutes_to_call is not None and minutes_to_call <= NEW_DEAL_WINDOW_MINUTES),
                "dial_attempts": dial_attempts,
                "disposition": most_recent.get("Outgoing_call_disposition"),
                "caller": self._caller_name(most_recent),
            }

        if elapsed_min is not None and elapsed_min <= NEW_DEAL_WINDOW_MINUTES:
            return {
                "status": "pending",
                "elapsed_minutes": round(elapsed_min, 1),
                "dial_attempts": dial_attempts,
                "caller": None,
            }

        return {
            "status": "missed",
            "elapsed_minutes": round(elapsed_min, 1) if elapsed_min is not None else None,
            "dial_attempts": dial_attempts,
            "caller": None,
        }

    def _classify_scheduled_call(self, rec: dict, calls: list[dict]) -> dict:
        # rec is now a Zoho Calls record — scheduled time is Call_Start_Time
        scheduled = self._parse_dt(rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"))
        if not scheduled:
            return {"status": "no_schedule", "dial_attempts": 0, "caller": None}

        now = datetime.now(timezone.utc)

        def mins_from_schedule(c):
            t = self._parse_dt(c.get("Call_Start_Time"))
            return abs((t - scheduled).total_seconds()) / 60 if t else float("inf")

        # All calls with a disposition for this phone = total dial attempts
        all_dialed = [c for c in calls if c.get("Outgoing_call_disposition")]
        dial_attempts = len(all_dialed)
        # Most recent dialed call drives the displayed disposition
        most_recent = max(all_dialed, key=lambda c: c.get("Call_Start_Time") or "") if all_dialed else None

        # Calls within the match window count toward schedule fulfillment
        nearby = [
            c for c in all_dialed
            if mins_from_schedule(c) <= SCHEDULED_CALL_MAX_MATCH_MINUTES
        ]

        if nearby:
            closest = min(nearby, key=mins_from_schedule)
            call_dt = self._parse_dt(closest.get("Call_Start_Time"))
            offset_min = (
                (call_dt - scheduled).total_seconds() / 60 if call_dt else None
            )
            # New classification:
            #   offset < -5      → early
            #   -5 ≤ offset ≤ 10 → completed (on_time)
            #   offset > 10      → completed (late)
            is_early = offset_min is not None and offset_min < -EARLY_BEFORE_MIN
            on_time  = (offset_min is not None
                        and -EARLY_BEFORE_MIN <= offset_min <= ON_TIME_AFTER_MIN)
            return {
                "status": "early" if is_early else "completed",
                "scheduled_time": rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"),
                "actual_call_time": closest.get("Call_Start_Time"),
                "offset_minutes": round(offset_min, 1) if offset_min is not None else None,
                "on_time": on_time,
                "dial_attempts": dial_attempts,
                "disposition": most_recent.get("Outgoing_call_disposition"),
                "caller": self._caller_name(most_recent),
            }

        minutes_until = (scheduled - now).total_seconds() / 60

        if minutes_until > 0:
            return {
                "status": "upcoming",
                "scheduled_time": rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"),
                "minutes_until": round(minutes_until, 1),
                "dial_attempts": dial_attempts,
                "caller": self._caller_name(most_recent) if most_recent else None,
            }

        minutes_overdue = -minutes_until
        return {
            "status": "missed" if minutes_overdue > SCHEDULED_CALL_TOLERANCE_MINUTES else "late",
            "scheduled_time": rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"),
            "actual_call_time": most_recent.get("Call_Start_Time") if most_recent else None,
            "minutes_overdue": round(minutes_overdue, 1),
            "dial_attempts": dial_attempts,
            "disposition": most_recent.get("Outgoing_call_disposition") if most_recent else None,
            "caller": self._caller_name(most_recent) if most_recent else None,
        }

    # ------------------------------------------------ scheduled call records

    def _fetch_scheduled_call_records_today(self) -> list[dict]:
        """Fetch today's Zoho Admin call records with 'scheduled' in Subject.

        Uses REST API search (not COQL) so Who_Id comes back as a full {id, name} dict.
        Window is today 00:00–23:59 in local time (TZ_OFFSET_HOURS), converted to UTC.
        """
        import logging
        log = logging.getLogger(__name__)
        now = datetime.now(timezone.utc)
        # Compute today's local midnight and end-of-day, then shift to UTC
        local_now = now + timedelta(hours=TZ_OFFSET_HOURS)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end   = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
        window_start = (local_start - timedelta(hours=TZ_OFFSET_HOURS)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        window_end   = (local_end   - timedelta(hours=TZ_OFFSET_HOURS)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

        all_records, page = [], 1
        while True:
            resp = requests.get(
                f"{self.base_url}/crm/v6/Calls/search",
                headers=self._headers(),
                params={
                    "criteria": (
                        f"((Subject:starts_with:Scheduled Call)or(Subject:starts_with:Call scheduled))"
                        f"and((Call_Start_Time:greater_equal:{window_start})"
                        f"and(Call_Start_Time:less_equal:{window_end}))"
                    ),
                    "fields": "id,Subject,Call_Start_Time,Who_Id,Owner",
                    "per_page": 200,
                    "page": page,
                },
                timeout=20,
            )
            if resp.status_code == 204 or not resp.ok:
                log.warning("Scheduled call search failed %s: %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            for call in data.get("data", []):
                owner = (call.get("Owner") or {})
                owner_name = owner.get("name") if isinstance(owner, dict) else owner
                if owner_name == "Zoho Admin":
                    all_records.append(call)
            if not data.get("info", {}).get("more_records"):
                break
            page += 1

        log.info("  → %d scheduled call records found (Zoho Admin, today±)", len(all_records))
        return all_records

    def _fetch_contact_phones(self, contact_ids: list[str]) -> dict[str, Optional[str]]:
        """Batch-fetch Phone for a list of Contact IDs. Returns {contact_id: normalized_phone}."""
        result: dict[str, Optional[str]] = {}
        BATCH = 100
        for i in range(0, len(contact_ids), BATCH):
            batch = contact_ids[i : i + BATCH]
            resp = requests.get(
                f"{self.base_url}/crm/v6/Contacts",
                headers=self._headers(),
                params={"ids": ",".join(batch), "fields": "id,Phone"},
                timeout=20,
            )
            if resp.ok:
                for c in resp.json().get("data", []):
                    result[c["id"]] = normalize_phone(c.get("Phone") or "")
        return result

    # Stages that indicate the deal was successfully closed/handled outside of RingCX
    # — long-overdue scheduled calls in these stages count as completed
    STAGE_MOVED_ON = {
        "Closed Won - Surgery Scheduled",
        "Unsubscribe",
        "Quote Sent",
        "Retainer Invoice Sent",
    }

    def _fetch_deal_stages_for_contacts(self, contact_ids: list[str]) -> dict[str, str]:
        """Returns {contact_id: stage} for each contact's most recently modified deal."""
        import logging
        log = logging.getLogger(__name__)
        result: dict[str, str] = {}
        BATCH = 8
        for i in range(0, len(contact_ids), BATCH):
            batch = contact_ids[i : i + BATCH]
            conditions = " or ".join(f"(Contact_Name:equals:{cid})" for cid in batch)
            resp = requests.get(
                f"{self.base_url}/crm/v6/Deals/search",
                headers=self._headers(),
                params={
                    "criteria": conditions,
                    "fields": "id,Stage,Contact_Name",
                    "sort_by": "Modified_Time",
                    "sort_order": "desc",
                    "per_page": 50,
                },
                timeout=20,
            )
            if resp.status_code == 204 or not resp.ok:
                log.warning("Deal stage lookup failed %s: %s", resp.status_code, resp.text[:200])
                continue
            for deal in resp.json().get("data", []):
                cn = deal.get("Contact_Name")
                cid = cn.get("id") if isinstance(cn, dict) else cn
                if cid and cid not in result:
                    result[cid] = deal.get("Stage") or ""
        log.info("  → deal stages fetched for %d contacts", len(result))
        return result

    def _fetch_ringcx_calls_today(self) -> list[dict]:
        """Fetch today's RingCX outbound calls that have Outgoing_call_disposition set.

        Subject pattern: 'Outbound Call to +1...'
        Uses COQL with today's date range.  Falls back to full-dump if COQL rejects Subject like.
        """
        import logging
        log = logging.getLogger(__name__)
        today_start, today_end = self._today_bounds()
        start_str = today_start.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        end_str   = today_end.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        all_calls, offset = [], 0
        while True:
            query = (
                "select id, Subject, Call_Start_Time, Outgoing_call_disposition, Call_Type, Owner "
                f"from Calls where Call_Start_Time between '{start_str}' and '{end_str}' "
                "and id is not null "
                f"order by Call_Start_Time desc limit 200 offset {offset}"
            )
            resp = requests.post(
                f"{self.base_url}/crm/v6/coql",
                headers=self._headers(),
                json={"select_query": query},
                timeout=20,
            )
            if resp.status_code == 204:
                break
            if not resp.ok:
                log.warning("RingCX calls fetch failed %s: %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            # Keep only calls with a disposition (RingCX auto-logged outbound attempts)
            all_calls.extend(
                c for c in data.get("data", []) if c.get("Outgoing_call_disposition")
            )
            if not data.get("info", {}).get("more_records"):
                break
            offset += 200

        log.info("  → %d RingCX calls with disposition fetched for today", len(all_calls))
        return all_calls

    # --------------------------------------------------------------- main

    def get_dashboard_data(self) -> dict:
        import logging
        log = logging.getLogger(__name__)

        # New Deals fetching is disabled — focusing on Scheduled Calls
        new_deals = []

        # ── Scheduled Calls ────────────────────────────────────────────────
        # Source A: Zoho Admin "Scheduled Call / Call scheduled" records → define the schedule
        log.info("Fetching today's scheduled call records (Zoho Admin)...")
        sched_call_records = self._fetch_scheduled_call_records_today()

        # Get contact phones for all scheduled records
        contact_ids = list({
            (c.get("Who_Id") or {}).get("id")
            for c in sched_call_records
            if isinstance(c.get("Who_Id"), dict) and c["Who_Id"].get("id")
        })
        log.info("  → fetching phones for %d contacts...", len(contact_ids))
        contact_phones = self._fetch_contact_phones(contact_ids)

        # Source B: RingCX outbound calls with disposition → actual dials
        log.info("Fetching today's RingCX outbound calls with disposition...")
        ringcx_calls = self._fetch_ringcx_calls_today()

        # Build phone → RingCX calls map (keyed by normalized last-10 digits extracted from Subject)
        ringcx_by_phone: dict[str, list[dict]] = {}
        for call in ringcx_calls:
            phone = self._phone_from_subject(call.get("Subject", "") or "")
            if phone:
                ringcx_by_phone.setdefault(phone, []).append(call)

        nd_phone_to_calls = {}

        def nd_calls_for(deal):
            n = normalize_phone(deal.get("Phone"))
            return nd_phone_to_calls.get(n, []) if n else []

        def sc_calls_for(sched_record):
            who = sched_record.get("Who_Id") or {}
            cid = who.get("id") if isinstance(who, dict) else None
            phone = contact_phones.get(cid) if cid else None
            return ringcx_by_phone.get(phone, []) if phone else []

        def deal_base(deal):
            owner = deal.get("Owner")
            owner_name = (
                owner.get("name") if isinstance(owner, dict)
                else owner if isinstance(owner, str)
                else None
            )
            return {
                "id": deal["id"],
                "name": deal.get("Deal_Name") or "—",
                "phone": deal.get("Phone"),
                "created_time": deal.get("Created_Time"),
                "scheduled_time": deal.get("Call_Scheduled_Date"),
                "owner": owner_name,
                "dialer_status": deal.get("DialerStatus"),
                "best_contact_time": deal.get("Best_Contact_Time"),
            }

        def sched_base(rec):
            who = rec.get("Who_Id") or {}
            cid = who.get("id") if isinstance(who, dict) else None
            phone = contact_phones.get(cid) if cid else None
            return {
                "id": rec["id"],
                "id_contact": cid,
                "name": who.get("name") if isinstance(who, dict) else "—",
                "phone": phone,
                "created_time": rec.get("Call_Start_Time"),
                "owner": "Zoho Admin",
            }

        nd_results = []
        for deal in new_deals:
            result = {**deal_base(deal), **self._classify_new_deal(deal, nd_calls_for(deal))}
            nd_results.append(result)

        sc_results = []
        for rec in sched_call_records:
            result = {**sched_base(rec), **self._classify_scheduled_call(rec, sc_calls_for(rec))}
            sc_results.append(result)

        # For scheduled calls missed > 90 min, look up deal stage to flag if deal moved on
        log.info("Checking deal stages for long-missed scheduled calls...")
        long_missed_contact_ids = list({
            r["id_contact"]
            for r in sc_results
            if r.get("status") in ("missed", "late")
            and (r.get("minutes_overdue") or 0) > 90
            and r.get("id_contact")
        })
        if long_missed_contact_ids:
            deal_stages = self._fetch_deal_stages_for_contacts(long_missed_contact_ids)
            for r in sc_results:
                cid = r.get("id_contact")
                if cid and cid in deal_stages:
                    stage = deal_stages[cid]
                    r["deal_stage"] = stage
                    if stage in self.STAGE_MOVED_ON:
                        # Treat as successfully closed via manual contact outside RingCX
                        r["deal_moved_on"] = True
                        r["status"] = "completed_via_deal"

        # New Deals: sort by created_time descending (most recent first)
        nd_results.sort(key=lambda d: d.get("created_time") or "", reverse=True)

        # Sort all scheduled calls by scheduled_time ascending — frontend splits past vs upcoming
        sc_results.sort(
            key=lambda d: d.get("scheduled_time") or d.get("created_time") or "",
        )

        def summary(results, mode="new"):
            # "early" and "completed_via_deal" count toward completion
            called = [r for r in results if r["status"] in ("completed", "early", "completed_via_deal")]
            completed = [r for r in results if r["status"] == "completed"]
            early     = [r for r in results if r["status"] == "early"]
            via_deal  = [r for r in results if r["status"] == "completed_via_deal"]
            on_time   = [r for r in completed if r.get("on_time")]
            missed    = [r for r in results if r["status"] == "missed"]
            s = {
                "total": len(results),
                "completed": len(called),
                "on_time": len(on_time),
                "early": len(early),
                "via_deal": len(via_deal),
                "missed": len(missed),
                "on_time_rate": round(len(on_time) / len(called) * 100) if called else None,
                "completion_rate": round(len(called) / len(results) * 100) if results else None,
            }
            if mode == "new":
                s["pending"] = len([r for r in results if r["status"] == "pending"])
            else:
                s["upcoming"] = len([r for r in results if r["status"] == "upcoming"])
                s["late"] = len([r for r in results if r["status"] == "late"])
            return s

        return {
            "new_deals": {"records": nd_results, "summary": summary(nd_results, "new")},
            "scheduled_calls": {"records": sc_results, "summary": summary(sc_results, "scheduled")},
        }

    # ───────────────────────── contact summary data ──────────────────────────

    def get_contact_summary_data(self, contact_id: str) -> dict:
        """Fetch all CRM data for a contact to feed into AI summary generation."""
        import logging
        log = logging.getLogger(__name__)

        # 1. Contact basic info
        contact = {}
        resp = requests.get(
            f"{self.base_url}/crm/v6/Contacts/{contact_id}",
            headers=self._headers(),
            params={"fields": "First_Name,Last_Name,Full_Name,Phone,Email,Lead_Source,Description,Title,Account_Name,Created_Time,Modified_Time"},
            timeout=15,
        )
        if resp.ok:
            d = resp.json().get("data", [{}])[0]
            contact = {
                "name": d.get("Full_Name") or f"{d.get('First_Name','')} {d.get('Last_Name','')}".strip(),
                "phone": d.get("Phone"), "email": d.get("Email"),
                "title": d.get("Title"), "lead_source": d.get("Lead_Source"),
                "description": d.get("Description"),
                "created": d.get("Created_Time"), "modified": d.get("Modified_Time"),
            }

        # 2. All calls linked to this contact (via COQL Who_Id)
        calls = []
        resp = requests.post(
            f"{self.base_url}/crm/v6/coql",
            headers=self._headers(),
            json={"select_query": (
                "select id, Subject, Call_Start_Time, Call_Type, Call_Duration_in_seconds, "
                "Outgoing_call_disposition, Description, Owner "
                f"from Calls where Who_Id = '{contact_id}' "
                "order by Call_Start_Time desc limit 50"
            )},
            timeout=20,
        )
        if resp.ok and resp.status_code != 204:
            for c in resp.json().get("data", []):
                owner = c.get("Owner") or {}
                calls.append({
                    "subject": c.get("Subject"), "time": c.get("Call_Start_Time"),
                    "type": c.get("Call_Type"),
                    "duration_sec": c.get("Call_Duration_in_seconds"),
                    "disposition": c.get("Outgoing_call_disposition"),
                    "description": (c.get("Description") or "")[:300],
                    "owner": owner.get("name") if isinstance(owner, dict) else owner,
                })

        # 3. Deals linked to this contact
        deals = []
        resp = requests.post(
            f"{self.base_url}/crm/v6/coql",
            headers=self._headers(),
            json={"select_query": (
                "select id, Deal_Name, Stage, Amount, Modified_Time, Created_Time, Description "
                f"from Deals where Contact_Name = '{contact_id}' "
                "order by Modified_Time desc limit 10"
            )},
            timeout=20,
        )
        if resp.ok and resp.status_code != 204:
            for d in resp.json().get("data", []):
                deals.append({
                    "name": d.get("Deal_Name"), "stage": d.get("Stage"),
                    "amount": d.get("Amount"), "modified": d.get("Modified_Time"),
                    "created": d.get("Created_Time"),
                    "description": (d.get("Description") or "")[:200],
                })

        # 4. Tasks / activities
        tasks = []
        resp = requests.post(
            f"{self.base_url}/crm/v6/coql",
            headers=self._headers(),
            json={"select_query": (
                "select id, Subject, Status, Due_Date, Description, Owner "
                f"from Tasks where Who_Id = '{contact_id}' "
                "order by Modified_Time desc limit 20"
            )},
            timeout=20,
        )
        if resp.ok and resp.status_code != 204:
            for t in resp.json().get("data", []):
                tasks.append({
                    "subject": t.get("Subject"), "status": t.get("Status"),
                    "due": t.get("Due_Date"),
                    "description": (t.get("Description") or "")[:200],
                })

        # 5. HelloSend SMS history linked to this contact
        sms_messages = []
        sms_module = "ringcentralbulksmsextensionforzohocrm__RingCentral_SMS_History"
        # Try common field-name variants — the module is a third-party extension
        for select_clause in (
            "select id, Name, Direction, Message, Status, Created_Time",
            "select id, Name, Created_Time",
        ):
            try:
                resp = requests.post(
                    f"{self.base_url}/crm/v6/coql",
                    headers=self._headers(),
                    json={"select_query": (
                        f"{select_clause} "
                        f"from {sms_module} "
                        f"where Who_Id = '{contact_id}' "
                        "order by Created_Time desc limit 30"
                    )},
                    timeout=15,
                )
                if resp.ok and resp.status_code != 204:
                    for s in resp.json().get("data", []):
                        sms_messages.append({
                            "time": s.get("Created_Time"),
                            "direction": s.get("Direction"),
                            "message": (s.get("Message") or s.get("Name") or "")[:300],
                            "status": s.get("Status"),
                        })
                    break  # query worked
                elif resp.status_code == 400:
                    # Try the simpler field set
                    continue
                else:
                    break
            except Exception as e:
                log.warning("SMS history fetch failed: %s", e)
                break

        log.info("Contact summary data: %d calls, %d deals, %d tasks, %d SMS for %s",
                 len(calls), len(deals), len(tasks), len(sms_messages), contact_id)
        return {
            "contact": contact,
            "calls": calls,
            "deals": deals,
            "tasks": tasks,
            "sms": sms_messages,
            "stats": {
                "total_calls": len(calls),
                "outbound_calls": sum(1 for c in calls if c.get("type") == "Outbound"),
                "calls_with_disposition": sum(1 for c in calls if c.get("disposition")),
                "total_deals": len(deals),
                "open_tasks": sum(1 for t in tasks if t.get("status") not in ("Completed", "Deferred")),
                "total_sms": len(sms_messages),
                "sms_inbound": sum(1 for s in sms_messages if (s.get("direction") or "").lower().startswith("in")),
                "sms_outbound": sum(1 for s in sms_messages if (s.get("direction") or "").lower().startswith("out")),
            },
        }
