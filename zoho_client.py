import os
import re
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

NEW_DEAL_WINDOW_MINUTES = 5
NEW_DEAL_LOOKBACK_HOURS = 48
# Timing thresholds for scheduled call classification:
#   < -EARLY_BEFORE_MIN  → "early"           (more than 15 min before scheduled)
#   -EARLY_BEFORE_MIN to +ON_TIME_AFTER_MIN  → "completed" on_time=True  (-15 to +15 min)
#   > +ON_TIME_AFTER_MIN → "completed" on_time=False  (late, > 15 min after)
EARLY_BEFORE_MIN     = int(os.getenv("EARLY_BEFORE_MIN", "15"))
ON_TIME_AFTER_MIN    = int(os.getenv("ON_TIME_AFTER_MIN", "15"))
# Earliest hour (local time) at which dialing begins. Calls scheduled before
# this hour are treated as if they were scheduled at this hour for classification.
DIAL_START_HOUR      = int(os.getenv("DIAL_START_HOUR", "9"))
# Legacy: kept only for compatibility with anything still reading SCHEDULED_TOLERANCE_MINUTES
SCHEDULED_CALL_TOLERANCE_MINUTES = ON_TIME_AFTER_MIN
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

    def _effective_scheduled(self, scheduled: datetime) -> datetime:
        """Return effective scheduled time, clamping pre-DIAL_START_HOUR calls to 9 AM local.

        Any call scheduled before DIAL_START_HOUR in local time (TZ_OFFSET_HOURS) is
        treated as if it were scheduled at exactly DIAL_START_HOUR that same day.
        Uses astimezone() so the math is correct regardless of the timezone the
        incoming datetime carries (Zoho can emit EDT -04:00, UTC, etc.).
        """
        local_tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
        # Ensure scheduled is tz-aware; assume UTC if naive
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        local_dt = scheduled.astimezone(local_tz)
        if local_dt.hour < DIAL_START_HOUR:
            clamped_local = local_dt.replace(
                hour=DIAL_START_HOUR, minute=0, second=0, microsecond=0
            )
            return clamped_local.astimezone(scheduled.tzinfo)
        return scheduled

    @staticmethod
    def _extract_recording_url(description: str) -> Optional[str]:
        """Extract a recording URL from a RingCX call description field."""
        if not description:
            return None
        urls = re.findall(r'https?://\S+', description)
        # Prefer URLs that look like recordings/audio
        for url in urls:
            low = url.lower().rstrip(".,;)")
            if any(kw in low for kw in ("recording", "listen", "audio", "media", "rec/", "playback")):
                return url.rstrip(".,;)")
        return urls[0].rstrip(".,;)") if urls else None

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
            "Call_Type, Owner, Created_Time, Description "
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
                "Call_Type, Owner, Created_Time, Description "
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

    def _fetch_all_calls_for_phones(
        self,
        phones: list[str],
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> dict[str, list[dict]]:
        """Returns a map of normalized last-10-digit phone → list of matching call records.

        Primary strategy: COQL batches of 5 phones using Subject like '%XXXXXXXXXX%'.
        Fallback: fetch all calls in window and match by Subject (capped at 1 000 records).

        If start_dt/end_dt are provided, uses that window; otherwise falls back
        to the default _call_window() (48 h lookback through end-of-today).
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

        if start_dt is not None and end_dt is not None:
            start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        else:
            start_str, end_str = self._call_window()
        # Zoho COQL only allows 2 "Subject like" OR conditions per query.
        BATCH = 2
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
        """Classify a scheduled call against logged RingCX/MVP calls.

        Core principle: actual_call_time is always the RingCX call (with
        disposition) logged in Zoho whose Call_Start_Time is closest to
        the scheduled time.  MVP calls (RingEX, no disposition) contribute
        to dial_attempts and last_attempt_time but never set actual_call_time.
        """
        scheduled = self._parse_dt(rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"))
        if not scheduled:
            return {"status": "no_schedule", "dial_attempts": 0, "caller": None}

        effective_scheduled = scheduled
        now = datetime.now(timezone.utc)

        def mins_from_schedule(c):
            t = self._parse_dt(c.get("Call_Start_Time"))
            return abs((t - effective_scheduled).total_seconds()) / 60 if t else float("inf")

        # A call only "belongs" to a scheduled slot if it happens on the SAME
        # local calendar date as the scheduled time.  This prevents yesterday's
        # 9 PM dial from matching today's 9 AM schedule.
        local_tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
        sched_local_date = effective_scheduled.astimezone(local_tz).date()
        def same_local_date(c):
            t = self._parse_dt(c.get("Call_Start_Time"))
            if not t:
                return False
            return t.astimezone(local_tz).date() == sched_local_date

        # ── Split calls by source ──────────────────────────────────────
        # RingCX calls (have disposition) — these are the source of truth
        # for actual_call_time and timing classification.
        all_dialed = [
            c for c in calls
            if c.get("Outgoing_call_disposition") and same_local_date(c)
        ]
        # RingCentral MVP calls ("Outgoing call to" subject, no disposition)
        # — count as dial attempts only; never drive actual_call_time.
        mvp_calls = [
            c for c in calls
            if not c.get("Outgoing_call_disposition")
            and "outgoing call to" in (c.get("Subject") or "").lower()
            and same_local_date(c)
        ]
        dial_attempts = len(all_dialed) + len(mvp_calls)

        most_recent_ringcx = (
            max(all_dialed, key=lambda c: c.get("Call_Start_Time") or "")
            if all_dialed else None
        )
        most_recent_any = most_recent_ringcx or (
            max(mvp_calls, key=lambda c: c.get("Call_Start_Time") or "")
            if mvp_calls else None
        )

        # Most recent attempt across both sources (for last_attempt_time)
        all_with_time = [
            c for c in (all_dialed + mvp_calls)
            if self._parse_dt(c.get("Call_Start_Time"))
        ]
        last_attempt = (
            max(all_with_time, key=lambda c: c.get("Call_Start_Time"))
            if all_with_time else None
        )

        # ── Find the RingCX call closest to scheduled time ─────────────
        # This is the single source of truth for actual_call_time.
        # No arbitrary time-window cap — same_local_date already scopes
        # to the correct calendar day.
        closest_ringcx = (
            min(all_dialed, key=mins_from_schedule) if all_dialed else None
        )

        if closest_ringcx:
            call_dt = self._parse_dt(closest_ringcx.get("Call_Start_Time"))
            offset_min = (
                (call_dt - effective_scheduled).total_seconds() / 60
                if call_dt else None
            )
            is_early = offset_min is not None and offset_min < -EARLY_BEFORE_MIN
            on_time  = (offset_min is not None
                        and -EARLY_BEFORE_MIN <= offset_min <= ON_TIME_AFTER_MIN)
            return {
                "status": "early" if is_early else "completed",
                "scheduled_time": rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"),
                "actual_call_time": closest_ringcx.get("Call_Start_Time"),
                "offset_minutes": round(offset_min, 1) if offset_min is not None else None,
                "on_time": on_time,
                "dial_attempts": dial_attempts,
                "disposition": closest_ringcx.get("Outgoing_call_disposition"),
                "caller": self._caller_name(closest_ringcx),
                "recording_url": self._extract_recording_url(
                    closest_ringcx.get("Description") or ""
                ),
                "logged_via": "ringcx",
                "last_attempt_time": (last_attempt.get("Call_Start_Time")
                                      if last_attempt else None),
            }

        # ── No RingCX call found on the same day ───────────────────────
        minutes_until = (effective_scheduled - now).total_seconds() / 60

        if minutes_until > 0:
            return {
                "status": "upcoming",
                "scheduled_time": rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"),
                "minutes_until": round(minutes_until, 1),
                "dial_attempts": dial_attempts,
                "caller": (self._caller_name(most_recent_any)
                           if most_recent_any else None),
            }

        # Overdue / missed — no RingCX call, so actual_call_time stays
        # empty.  MVP calls are surfaced via last_attempt_time and the
        # mvp_only flag so the dashboard can still show dial activity.
        minutes_overdue = -minutes_until
        return {
            "status": ("missed" if minutes_overdue > SCHEDULED_CALL_TOLERANCE_MINUTES
                       else "late"),
            "scheduled_time": rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"),
            "actual_call_time": None,
            "minutes_overdue": round(minutes_overdue, 1),
            "dial_attempts": dial_attempts,
            "mvp_only": len(mvp_calls) > 0,
            "disposition": (most_recent_ringcx.get("Outgoing_call_disposition")
                            if most_recent_ringcx else None),
            "caller": self._caller_name(most_recent_any),
            "recording_url": None,
            "logged_via": "mvp" if mvp_calls else None,
            "last_attempt_time": (last_attempt.get("Call_Start_Time")
                                  if last_attempt else None),
        }

    # ------------------------------------------------ scheduled call records

    def _fetch_scheduled_call_records_today(
        self,
        window_start_dt: Optional[datetime] = None,
        window_end_dt: Optional[datetime] = None,
    ) -> list[dict]:
        """Fetch Zoho Admin 'Scheduled Call' records within the given UTC window.

        If window_start_dt / window_end_dt are None, defaults to today in local time.
        Uses REST API search so Who_Id comes back as a full {id, name} dict.
        """
        import logging
        log = logging.getLogger(__name__)
        if window_start_dt is not None:
            window_start = window_start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            window_end   = window_end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        else:
            now = datetime.now(timezone.utc)
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
                    "fields": "id,Subject,Call_Start_Time,Created_Time,Modified_Time,Who_Id,What_Id,Owner,Description,Call_Status",
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

    # Stages where the deal owner's name should be surfaced on the record
    OWNER_VISIBLE_STAGES = {
        "Quote Sent",
        "Retainer Invoice Sent",
        "Closed Won - Surgery Scheduled",
        "Payment Received",
        "Payment Recieved",  # Zoho typo variant
    }

    def _fetch_deal_stages_by_ids(self, deal_ids: list[str]) -> dict[str, dict]:
        """Returns {deal_id: {"stage": str, "owner": str}} for the specific deals."""
        import logging
        log = logging.getLogger(__name__)
        result: dict[str, dict] = {}
        BATCH = 50
        for i in range(0, len(deal_ids), BATCH):
            batch = deal_ids[i : i + BATCH]
            ids_param = ",".join(batch)
            resp = requests.get(
                f"{self.base_url}/crm/v6/Deals",
                headers=self._headers(),
                params={
                    "ids": ids_param,
                    "fields": "id,Stage,Owner,Language",
                },
                timeout=20,
            )
            if resp.status_code == 204 or not resp.ok:
                log.warning("Deal ID lookup failed %s: %s", resp.status_code, resp.text[:200])
                continue
            for deal in resp.json().get("data", []):
                did = deal.get("id")
                if did:
                    owner = deal.get("Owner") or {}
                    owner_name = owner.get("name") if isinstance(owner, dict) else (owner or "")
                    lang_raw = (deal.get("Language") or "").strip()
                    result[did] = {
                        "stage": deal.get("Stage") or "",
                        "owner": owner_name,
                        "language": lang_raw if lang_raw and lang_raw != "Unselected" else "",
                    }
        log.info("  → deal stages fetched by ID for %d deals", len(result))
        return result

    def _fetch_deal_stages_for_contacts(self, contact_ids: list[str]) -> dict[str, dict]:
        """Returns {contact_id: {"stage": str, "owner": str}} for each contact's most recently modified deal."""
        import logging
        log = logging.getLogger(__name__)
        result: dict[str, dict] = {}
        BATCH = 15
        for i in range(0, len(contact_ids), BATCH):
            batch = contact_ids[i : i + BATCH]
            conditions = " or ".join(f"(Contact_Name:equals:{cid})" for cid in batch)
            resp = requests.get(
                f"{self.base_url}/crm/v6/Deals/search",
                headers=self._headers(),
                params={
                    "criteria": conditions,
                    "fields": "id,Stage,Contact_Name,Owner",
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
                    owner = deal.get("Owner") or {}
                    owner_name = owner.get("name") if isinstance(owner, dict) else (owner or "")
                    result[cid] = {
                        "stage": deal.get("Stage") or "",
                        "owner": owner_name,
                    }
        log.info("  → deal stages fetched for %d contacts", len(result))
        return result

    def _fetch_ringcx_calls_today(
        self,
        window_start_dt: Optional[datetime] = None,
        window_end_dt: Optional[datetime] = None,
    ) -> list[dict]:
        """Fetch RingCX outbound calls with a disposition within the given UTC window.

        If window_start_dt / window_end_dt are None, defaults to today's bounds.
        Uses the REST /Calls/search endpoint (works with ZohoCRM.modules.ALL scope).
        """
        import logging
        log = logging.getLogger(__name__)
        if window_start_dt is not None:
            start_str = window_start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            end_str   = window_end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        else:
            today_start, today_end = self._today_bounds()
            start_str = today_start.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            end_str   = today_end.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        all_calls, page = [], 1
        while True:
            resp = requests.get(
                f"{self.base_url}/crm/v6/Calls/search",
                headers=self._headers(),
                params={
                    "criteria": (
                        f"((Call_Start_Time:greater_equal:{start_str})"
                        f"and(Call_Start_Time:less_equal:{end_str}))"
                    ),
                    "fields": "id,Subject,Call_Start_Time,Outgoing_call_disposition,Call_Type,Owner,Description",
                    "per_page": 200,
                    "page": page,
                },
                timeout=20,
            )
            if resp.status_code == 204:
                break
            if not resp.ok:
                log.warning("RingCX calls fetch failed %s: %s", resp.status_code, resp.text[:200])
                break
            data = resp.json()
            # Keep RingCX calls (have Outgoing_call_disposition) AND
            # RingCentral MVP calls ("Outgoing call to" subject, no disposition)
            for c in data.get("data", []):
                subj = (c.get("Subject") or "").lower()
                if c.get("Outgoing_call_disposition") or "outgoing call to" in subj:
                    all_calls.append(c)
            if not data.get("info", {}).get("more_records"):
                break
            page += 1

        log.info("  → %d RingCX calls with disposition fetched for today", len(all_calls))
        return all_calls

    # --------------------------------------------------------------- main

    def get_dashboard_data(
        self,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> dict:
        import logging
        log = logging.getLogger(__name__)

        # New Deals fetching is disabled — focusing on Scheduled Calls
        new_deals = []

        # ── Scheduled Calls ────────────────────────────────────────────────
        # Source A: Zoho Admin "Scheduled Call / Call scheduled" records → define the schedule
        log.info("Fetching scheduled call records (window: %s → %s)...",
                 start_dt.date() if start_dt else "today",
                 end_dt.date() if end_dt else "today")
        sched_call_records = self._fetch_scheduled_call_records_today(start_dt, end_dt)

        # Get contact phones for all scheduled records
        contact_ids = list({
            (c.get("Who_Id") or {}).get("id")
            for c in sched_call_records
            if isinstance(c.get("Who_Id"), dict) and c["Who_Id"].get("id")
        })
        log.info("  → fetching phones for %d contacts...", len(contact_ids))
        contact_phones = self._fetch_contact_phones(contact_ids)

        # Source B: Outbound calls (RingCX + MVP) for the phones we care about.
        # Uses targeted COQL queries by phone number in Subject, avoiding the
        # Zoho search API's 2000-record limit that caused missed calls.
        all_phones = [p for p in contact_phones.values() if p]
        log.info("Fetching outbound calls for %d unique phones...", len(set(all_phones)))
        ringcx_by_phone = self._fetch_all_calls_for_phones(all_phones, start_dt, end_dt)

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
            # What_Id links to the related record (usually a Deal)
            what = rec.get("What_Id") or {}
            what_id = what.get("id") if isinstance(what, dict) else None
            # Detect last-minute scheduling: created_time vs scheduled_time
            sched_dt = self._parse_dt(rec.get("Call_Start_Time"))
            record_created_dt = self._parse_dt(rec.get("Created_Time"))
            last_minute = False
            if sched_dt and record_created_dt:
                lead_min = (sched_dt - record_created_dt).total_seconds() / 60
                last_minute = 0 <= lead_min <= 15

            return {
                "id": rec["id"],
                "id_contact": cid,
                "id_deal": what_id,  # deal linked directly to this call
                "name": who.get("name") if isinstance(who, dict) else "—",
                "phone": phone,
                "created_time": rec.get("Call_Start_Time"),
                "record_created": rec.get("Created_Time"),
                "record_modified": rec.get("Modified_Time"),
                "last_minute": last_minute,
                "owner": "Zoho Admin",
                "call_status": rec.get("Call_Status") or "",
                "sched_description": rec.get("Description") or "",
            }

        nd_results = []
        for deal in new_deals:
            result = {**deal_base(deal), **self._classify_new_deal(deal, nd_calls_for(deal))}
            nd_results.append(result)

        sc_results = []
        for rec in sched_call_records:
            result = {**sched_base(rec), **self._classify_scheduled_call(rec, sc_calls_for(rec))}
            sc_results.append(result)

        # Look up deal stages ONLY for calls with a direct deal link (What_Id).
        # We no longer fall back to the contact's most-recent deal, because that
        # can show the wrong stage when a contact has multiple deals.
        linked_deal_ids = list({r["id_deal"] for r in sc_results if r.get("id_deal")})

        deal_by_id_map = {}
        if linked_deal_ids:
            log.info("Fetching deal stages by deal ID for %d linked deals...", len(linked_deal_ids))
            deal_by_id_map = self._fetch_deal_stages_by_ids(linked_deal_ids)

        for r in sc_results:
            info = None
            did = r.get("id_deal")
            if did and did in deal_by_id_map:
                info = deal_by_id_map[did]

            if info:
                stage = info["stage"]
                owner = info["owner"]
                # Surface the stage whenever it's moved beyond the initial "New Deal" stage
                if stage and stage != "New Deal":
                    r["deal_stage"] = stage
                # Surface the deal owner for high-value stages
                if stage in self.OWNER_VISIBLE_STAGES and owner:
                    r["deal_owner"] = owner
                # Language from deal record
                if info.get("language"):
                    r["language"] = info["language"]
                # Flag that the deal has moved on, but don't change call status —
                # status should reflect whether a call was actually made.
                if (stage
                        and stage not in ("New Deal", "Call Scheduled")
                        and r.get("status") in ("missed", "late")):
                    r["deal_moved_on"] = True

        # Workflow auto-completion detection: for overdue calls with 0 dials,
        # check if the Zoho scheduled call was marked Completed by workflow
        # (either Call_Status == "Completed" or description contains workflow keywords).
        WORKFLOW_KEYWORDS = (
            "completed by workflow",
            "auto-completed",
            "auto completed",
            "removed from queue",
            "workflow completed",
            "stage changed",
            "moved to",
        )
        for r in sc_results:
            if r.get("status") in ("missed", "late") and r.get("dial_attempts", 0) == 0:
                call_status = (r.get("call_status") or "").strip()
                desc = (r.get("sched_description") or "").lower()
                if call_status == "Completed" or any(kw in desc for kw in WORKFLOW_KEYWORDS):
                    r["completed_via_workflow"] = True
                    r["status"] = "completed_via_workflow"

        # New Deals: sort by created_time descending (most recent first)
        nd_results.sort(key=lambda d: d.get("created_time") or "", reverse=True)

        # Sort all scheduled calls by scheduled_time ascending — frontend splits past vs upcoming
        sc_results.sort(
            key=lambda d: d.get("scheduled_time") or d.get("created_time") or "",
        )

        def summary(results, mode="new"):
            # "early" and "completed_via_workflow" count toward completion
            called = [r for r in results if r["status"] in ("completed", "early", "completed_via_workflow")]
            completed    = [r for r in results if r["status"] == "completed"]
            early        = [r for r in results if r["status"] == "early"]
            via_workflow = [r for r in results if r["status"] == "completed_via_workflow"]
            deal_moved   = [r for r in results if r.get("deal_moved_on")]
            on_time      = [r for r in completed if r.get("on_time")]
            missed       = [r for r in results if r["status"] == "missed"]
            s = {
                "total": len(results),
                "completed": len(called),
                "on_time": len(on_time),
                "early": len(early),
                "via_deal": len(deal_moved),  # count for display, not a status
                "via_workflow": len(via_workflow),
                "missed": len(missed),
                "on_time_rate": round((len(on_time) + len(early)) / len(called) * 100) if called else None,
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

        # 2. All calls linked to this contact (via REST search; no COQL scope needed)
        calls = []
        resp = requests.get(
            f"{self.base_url}/crm/v6/Calls/search",
            headers=self._headers(),
            params={
                "criteria": f"(Who_Id:equals:{contact_id})",
                "fields": "id,Subject,Call_Start_Time,Call_Type,Call_Duration_in_seconds,Outgoing_call_disposition,Description,Owner",
                "per_page": 50,
            },
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
        resp = requests.get(
            f"{self.base_url}/crm/v6/Deals/search",
            headers=self._headers(),
            params={
                "criteria": f"(Contact_Name:equals:{contact_id})",
                "fields": "id,Deal_Name,Stage,Amount,Modified_Time,Created_Time,Description",
                "per_page": 10,
            },
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
        resp = requests.get(
            f"{self.base_url}/crm/v6/Tasks/search",
            headers=self._headers(),
            params={
                "criteria": f"(Who_Id:equals:{contact_id})",
                "fields": "id,Subject,Status,Due_Date,Description,Owner",
                "per_page": 20,
            },
            timeout=20,
        )
        if resp.ok and resp.status_code != 204:
            for t in resp.json().get("data", []):
                tasks.append({
                    "subject": t.get("Subject"), "status": t.get("Status"),
                    "due": t.get("Due_Date"),
                    "description": (t.get("Description") or "")[:200],
                })

        # 5. HelloSend / RingCentral SMS history (CustomModule23)
        # Module API name: ringcentralbulksmsextensionforzohocrm__RingCentral_SMS_History
        # Key fields discovered from the module:
        #   To:             ringcentralbulksmsextensionforzohocrm__To          (recipient phone)
        #   From:           ringcentralbulksmsextensionforzohocrm__From_Number (sender phone)
        #   SMS body:       ringcentralbulksmsextensionforzohocrm__SMS
        #   Direction:      ringcentralbulksmsextensionforzohocrm__SMS_Type    (Outbound/Inbound)
        #   Contact link:   ringcentralbulksmsextensionforzohocrm__Contact_Lookup
        #   Channel:        ringcentralbulksmsextensionforzohocrm__Channel
        #   Sent via:       ringcentralbulksmsextensionforzohocrm__SMS_Sent_Via
        SMS_PREFIX = "ringcentralbulksmsextensionforzohocrm__"
        sms_messages = []
        sms_module = f"{SMS_PREFIX}RingCentral_SMS_History"
        contact_phone = normalize_phone(contact.get("phone") or "")
        try:
            sms_records = []

            # Strategy 1: Search by Contact_Lookup (direct link — most reliable)
            contact_lookup_field = f"{SMS_PREFIX}Contact_Lookup"
            resp = requests.get(
                f"{self.base_url}/crm/v6/{sms_module}/search",
                headers=self._headers(),
                params={
                    "criteria": f"({contact_lookup_field}:equals:{contact_id})",
                    "per_page": 50,
                    "sort_by": "Created_Time",
                    "sort_order": "desc",
                },
                timeout=15,
            )
            if resp.ok:
                sms_records = resp.json().get("data", [])
                log.info("HelloSend: %d SMS via Contact_Lookup for %s", len(sms_records), contact_id)

            # Strategy 2: If no results via lookup, search by phone number
            # (catches messages where Contact_Lookup wasn't populated)
            if not sms_records and contact_phone:
                to_field = f"{SMS_PREFIX}To"
                from_field = f"{SMS_PREFIX}From_Number"
                phone_variants = [f"+1{contact_phone}", contact_phone]
                # Search To field (outbound to contact) and From field (inbound from contact)
                for field in [to_field, from_field]:
                    or_terms = "or".join(f"({field}:equals:{v})" for v in phone_variants)
                    criteria = f"({or_terms})" if len(phone_variants) > 1 else or_terms
                    resp = requests.get(
                        f"{self.base_url}/crm/v6/{sms_module}/search",
                        headers=self._headers(),
                        params={
                            "criteria": criteria,
                            "per_page": 50,
                            "sort_by": "Created_Time",
                            "sort_order": "desc",
                        },
                        timeout=15,
                    )
                    if resp.ok:
                        new_recs = resp.json().get("data", [])
                        # Merge, deduplicate by ID
                        existing_ids = {r["id"] for r in sms_records}
                        for r in new_recs:
                            if r["id"] not in existing_ids:
                                sms_records.append(r)
                                existing_ids.add(r["id"])
                if sms_records:
                    log.info("HelloSend: %d SMS via phone match for %s", len(sms_records), contact_phone)
                    # Re-sort merged results by Created_Time desc
                    sms_records.sort(key=lambda r: r.get("Created_Time", ""), reverse=True)

            sms_body_field = f"{SMS_PREFIX}SMS"
            sms_type_field = f"{SMS_PREFIX}SMS_Type"
            sms_via_field  = f"{SMS_PREFIX}SMS_Sent_Via"
            for s in sms_records:
                msg = s.get(sms_body_field) or s.get("Name") or ""
                sms_type = s.get(sms_type_field) or ""  # "Outbound" or "Inbound"
                direction = "outbound" if "outbound" in sms_type.lower() else (
                    "inbound" if "inbound" in sms_type.lower() else sms_type
                )
                owner = s.get("Owner") or {}
                owner_name = owner.get("name") if isinstance(owner, dict) else (owner or "")
                sms_messages.append({
                    "time": s.get("Created_Time"),
                    "direction": direction,
                    "message": str(msg)[:300],
                    "status": s.get(sms_via_field) or "",
                    "owner": owner_name,
                })
        except Exception as e:
            log.warning("SMS history fetch failed: %s", e)

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

    # ───────────────────────── SMS history search ──────────────────────────

    def search_sms_history(self, phone: str) -> list:
        """Search HelloSend SMS module by phone number.

        Uses two strategies:
        1. COQL query on Name field (contains contact phone)
        2. REST search with word parameter as fallback
        """
        log = logging.getLogger(__name__)
        SMS_PREFIX = "ringcentralbulksmsextensionforzohocrm__"
        sms_module = f"{SMS_PREFIX}RingCentral_SMS_History"
        to_field = f"{SMS_PREFIX}To"
        from_field = f"{SMS_PREFIX}From_Number"
        sms_body_field = f"{SMS_PREFIX}SMS"
        sms_type_field = f"{SMS_PREFIX}SMS_Type"
        sms_via_field = f"{SMS_PREFIX}SMS_Sent_Via"

        digits = normalize_phone(phone)
        if not digits:
            return []

        sms_records = []
        existing_ids = set()

        # Strategy 1: COQL (Name field contains contact phone)
        try:
            query = (
                f"select Name, {to_field}, {from_field}, {sms_body_field}, "
                f"{sms_type_field}, {sms_via_field}, Created_Time, Owner "
                f"from {sms_module} "
                f"where Name like '%{digits}%' "
                f"order by Created_Time desc limit 200"
            )
            log.info("SMS COQL: searching Name like %%%s%%", digits)
            resp = requests.post(
                f"{self.base_url}/crm/v6/coql",
                headers=self._headers(),
                json={"select_query": query},
                timeout=20,
            )
            log.info("SMS COQL response: %d len=%d", resp.status_code, len(resp.text))
            if resp.ok:
                rdata = resp.json()
                for r in rdata.get("data", []):
                    if r["id"] not in existing_ids:
                        sms_records.append(r)
                        existing_ids.add(r["id"])
                log.info("SMS COQL: %d records via Name", len(sms_records))
            else:
                log.warning("SMS COQL failed %d: %s", resp.status_code, resp.text[:500])
        except Exception as e:
            log.warning("SMS COQL error: %s", e, exc_info=True)

        # Strategy 2: REST word search as fallback
        if not sms_records:
            for variant in [f"+1{digits}", digits]:
                try:
                    resp = requests.get(
                        f"{self.base_url}/crm/v6/{sms_module}/search",
                        headers=self._headers(),
                        params={"word": variant, "per_page": 100},
                        timeout=15,
                    )
                    if resp.ok:
                        for r in resp.json().get("data", []):
                            if r["id"] not in existing_ids:
                                sms_records.append(r)
                                existing_ids.add(r["id"])
                    log.info("SMS REST word=%s: %d total", variant, len(sms_records))
                    if sms_records:
                        break
                except Exception as e:
                    log.warning("SMS REST search error: %s", e)

        sms_records.sort(key=lambda r: r.get("Created_Time", ""), reverse=True)

        messages = []
        for s in sms_records:
            msg = s.get(sms_body_field) or s.get("Name") or ""
            sms_type = s.get(sms_type_field) or ""
            direction = "outbound" if "outbound" in sms_type.lower() else (
                "inbound" if "inbound" in sms_type.lower() else sms_type
            )
            owner = s.get("Owner") or {}
            owner_name = owner.get("name") if isinstance(owner, dict) else (owner or "")
            messages.append({
                "time": s.get("Created_Time"),
                "direction": direction,
                "message": str(msg)[:500],
                "status": s.get(sms_via_field) or "",
                "owner": owner_name,
                "from_number": s.get(from_field) or "",
                "to_number": s.get(to_field) or "",
            })

        log.info("SMS search: %d messages for %s", len(messages), digits)
        return messages

    # ───────────────────────── schedule call support ────────────────────────

    def get_crm_owners(self) -> list[dict]:
        """Fetch CRM users from Deal owners (REST gives full names).

        The /users endpoint requires ZohoCRM.users.READ scope, which the
        current token doesn't have.  Instead, we pull unique Owner names
        from the "Call Scheduled" stage deals via REST search.
        """
        import logging
        log = logging.getLogger(__name__)
        owners: dict[str, str] = {}
        page = 1
        while page <= 3:
            resp = requests.get(
                f"{self.base_url}/crm/v6/Deals/search",
                headers=self._headers(),
                params={
                    "criteria": "(Stage:equals:Call Scheduled)",
                    "fields": "id,Owner",
                    "per_page": 200,
                    "page": page,
                },
                timeout=20,
            )
            if resp.status_code == 204 or not resp.ok:
                break
            for d in resp.json().get("data", []):
                o = d.get("Owner") or {}
                if isinstance(o, dict) and o.get("id") and o.get("name"):
                    owners[o["id"]] = o["name"]
            if not resp.json().get("info", {}).get("more_records"):
                break
            page += 1
        log.info("CRM owners: %d users found", len(owners))
        return [
            {"id": oid, "name": name}
            for oid, name in sorted(owners.items(), key=lambda x: x[1])
            if name != "Zoho Admin"
        ]

    def search_contacts(self, query: str) -> list[dict]:
        """Search contacts by name or phone. Returns up to 10 matches.

        Uses the Zoho 'word' search parameter which matches across
        multiple fields (name, phone, email).  Falls back to
        Phone:starts_with for digit-heavy queries.
        """
        results: list[dict] = []
        seen_ids: set[str] = set()

        def _add(data_list: list) -> None:
            for c in data_list:
                cid = c.get("id")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    results.append({
                        "id": cid,
                        "name": c.get("Full_Name") or "",
                        "phone": c.get("Phone") or "",
                        "email": c.get("Email") or "",
                    })

        # Strategy 1: word search (works for names and phone numbers)
        resp = requests.get(
            f"{self.base_url}/crm/v6/Contacts/search",
            headers=self._headers(),
            params={
                "word": query,
                "fields": "id,Full_Name,Phone,Email",
                "per_page": 10,
            },
            timeout=15,
        )
        if resp.ok and resp.status_code != 204:
            _add(resp.json().get("data", []))

        # Strategy 2: if query has digits, also try Phone:starts_with
        digits = re.sub(r"\D", "", query)
        if len(digits) >= 3 and len(results) < 10:
            resp2 = requests.get(
                f"{self.base_url}/crm/v6/Contacts/search",
                headers=self._headers(),
                params={
                    "criteria": f"(Phone:starts_with:{digits})",
                    "fields": "id,Full_Name,Phone,Email",
                    "per_page": 10,
                },
                timeout=15,
            )
            if resp2.ok and resp2.status_code != 204:
                _add(resp2.json().get("data", []))

        return results[:10]

    def get_deals_for_contact(self, contact_id: str) -> list[dict]:
        """Get deals linked to a contact for the deal selector."""
        resp = requests.get(
            f"{self.base_url}/crm/v6/Deals/search",
            headers=self._headers(),
            params={
                "criteria": f"(Contact_Name:equals:{contact_id})",
                "fields": "id,Deal_Name,Stage,Owner",
                "sort_by": "Modified_Time",
                "sort_order": "desc",
                "per_page": 10,
            },
            timeout=15,
        )
        if resp.status_code == 204 or not resp.ok:
            return []
        results = []
        for d in resp.json().get("data", []):
            owner = d.get("Owner") or {}
            results.append({
                "id": d.get("id"),
                "name": d.get("Deal_Name") or "",
                "stage": d.get("Stage") or "",
                "owner": owner.get("name") if isinstance(owner, dict) else "",
            })
        return results

    def create_scheduled_call(
        self,
        contact_id: str,
        contact_name: str,
        call_time: str,
        deal_id: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> dict:
        """Create a scheduled call record in Zoho CRM.

        Args:
            contact_id: Zoho Contact record ID
            contact_name: Contact display name (for Subject)
            call_time: ISO 8601 datetime string
            deal_id: Optional Zoho Deal ID to link
            owner_id: Optional Zoho user ID for Call Owner
        """
        call_data: dict = {
            "Subject": f"Scheduled Call: {contact_name}",
            "Call_Type": "Outbound",
            "Call_Start_Time": call_time,
            "$se_module": "Deals" if deal_id else "Contacts",
            "Who_Id": contact_id,
        }
        if deal_id:
            call_data["What_Id"] = deal_id
        if owner_id:
            call_data["Owner"] = owner_id

        resp = requests.post(
            f"{self.base_url}/crm/v6/Calls",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"data": [call_data]},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json().get("data", [{}])[0]
        if result.get("status") != "success":
            raise RuntimeError(f"Call creation failed: {result.get('message', 'unknown error')}")
        return {
            "id": result.get("details", {}).get("id"),
            "status": "created",
        }

