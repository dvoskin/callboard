import os
import re
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

NEW_DEAL_WINDOW_MINUTES = 5
NEW_DEAL_LOOKBACK_HOURS = 48
EARLY_BEFORE_MIN     = int(os.getenv("EARLY_BEFORE_MIN", "5"))
ON_TIME_AFTER_MIN    = int(os.getenv("ON_TIME_AFTER_MIN", "10"))
DIAL_START_HOUR      = int(os.getenv("DIAL_START_HOUR", "9"))
SCHEDULED_CALL_TOLERANCE_MINUTES = ON_TIME_AFTER_MIN
SCHEDULED_CALL_MAX_MATCH_MINUTES = int(os.getenv("SCHEDULED_MAX_MATCH_MINUTES", "720"))
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
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return today_start, today_end

    def _get_deals_by_stage(self, stage: str) -> list:
        import logging
        select_fields = (
            "id, Deal_Name, Phone, Created_Time, Stage, Contact_Name, Owner, "
            "DialAttempts, DialerStatus, Call_Scheduled_Date, Best_Contact_Time"
        )
        now = datetime.now(timezone.utc)
        today_start, today_end = self._today_bounds()
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
                if stage == "New Deal":
                    if created and created < nd_start:
                        done = True
                        break
                    if created and created >= nd_start:
                        all_deals.append(deal)
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
        local_dt = scheduled + timedelta(hours=TZ_OFFSET_HOURS)
        if local_dt.hour < DIAL_START_HOUR:
            local_start = local_dt.replace(
                hour=DIAL_START_HOUR, minute=0, second=0, microsecond=0
            )
            return local_start - timedelta(hours=TZ_OFFSET_HOURS)
        return scheduled

    @staticmethod
    def _extract_recording_url(description: str) -> Optional[str]:
        if not description:
            return None
        urls = re.findall(r'https?://\S+', description)
        for url in urls:
            low = url.lower().rstrip(".,;)")
            if any(kw in low for kw in ("recording", "listen", "audio", "media", "rec/", "playback")):
                return url.rstrip(".,;)")
        return urls[0].rstrip(".,;)") if urls else None

    @staticmethod
    def _phone_from_subject(subject: str) -> Optional[str]:
        if not subject:
            return None
        digits = re.sub(r"\D", "", subject)
        return digits[-10:] if len(digits) >= 10 else None

    def _call_window(self) -> tuple:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=NEW_DEAL_LOOKBACK_HOURS)
        _, today_end = self._today_bounds()
        return (
            window_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            today_end.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        )

    def _fetch_calls_batch_by_subject(
        self, phones: list, start_str: str, end_str: str
    ) -> Optional[list]:
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
            return None
        return resp.json().get("data", [])

    def _fetch_calls_full_dump(self, start_str: str, end_str: str) -> list:
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

    def _fetch_all_calls_for_phones(self, phones: list) -> dict:
        import logging
        log = logging.getLogger(__name__)

        phone_map = {}
        unique_phones = []
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

    def _parse_dt(self, value) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _caller_name(call) -> Optional[str]:
        if not call:
            return None
        owner = call.get("Owner")
        if isinstance(owner, dict):
            return owner.get("name")
        return owner if isinstance(owner, str) else None

    def _classify_new_deal(self, deal: dict, calls: list) -> dict:
        created = self._parse_dt(deal.get("Created_Time"))
        now = datetime.now(timezone.utc)
        elapsed_min = (now - created).total_seconds() / 60 if created else None

        dialed = [c for c in calls if c.get("Outgoing_call_disposition")]
        dial_attempts = len(dialed)
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

    def _classify_scheduled_call(self, rec: dict, calls: list) -> dict:
        scheduled = self._parse_dt(rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"))
        if not scheduled:
            return {"status": "no_schedule", "dial_attempts": 0, "caller": None}

        effective_scheduled = self._effective_scheduled(scheduled)
        now = datetime.now(timezone.utc)

        def mins_from_schedule(c):
            t = self._parse_dt(c.get("Call_Start_Time"))
            return abs((t - effective_scheduled).total_seconds()) / 60 if t else float("inf")

        all_dialed = [c for c in calls if c.get("Outgoing_call_disposition")]
        mvp_calls = [
            c for c in calls
            if not c.get("Outgoing_call_disposition")
            and "outgoing call to" in (c.get("Subject") or "").lower()
        ]
        dial_attempts = len(all_dialed) + len(mvp_calls)
        most_recent = max(all_dialed, key=lambda c: c.get("Call_Start_Time") or "") if all_dialed else None
        most_recent_any = most_recent or (
            max(mvp_calls, key=lambda c: c.get("Call_Start_Time") or "") if mvp_calls else None
        )

        nearby = [
            c for c in all_dialed
            if mins_from_schedule(c) <= SCHEDULED_CALL_MAX_MATCH_MINUTES
        ]

        if nearby:
            closest = min(nearby, key=mins_from_schedule)
            call_dt = self._parse_dt(closest.get("Call_Start_Time"))
            offset_min = (
                (call_dt - effective_scheduled).total_seconds() / 60 if call_dt else None
            )
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
                "recording_url": self._extract_recording_url(closest.get("Description") or ""),
            }

        minutes_until = (effective_scheduled - now).total_seconds() / 60

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
            "actual_call_time": most_recent_any.get("Call_Start_Time") if most_recent_any else None,
            "minutes_overdue": round(minutes_overdue, 1),
            "dial_attempts": dial_attempts,
            "mvp_only": len(all_dialed) == 0 and len(mvp_calls) > 0,
            "disposition": most_recent.get("Outgoing_call_disposition") if most_recent else None,
            "caller": self._caller_name(most_recent or most_recent_any),
            "recording_url": self._extract_recording_url((most_recent.get("Description") or "") if most_recent else ""),
        }

    # ------------------------------------------------ scheduled call records

    def _fetch_scheduled_call_records_today(
        self,
        window_start_dt=None,
        window_end_dt=None,
    ) -> list:
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
                    "fields": "id,Subject,Call_Start_Time,Who_Id,Owner,Description,Call_Status",
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

    def _fetch_contact_phones(self, contact_ids: list) -> dict:
        result = {}
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

    STAGE_MOVED_ON = {
        "Closed Won - Surgery Scheduled",
        "Unsubscribe",
        "Quote Sent",
        "Retainer Invoice Sent",
    }

    OWNER_VISIBLE_STAGES = {
        "Quote Sent",
        "Retainer Invoice Sent",
        "Closed Won - Surgery Scheduled",
        "Payment Received",
        "Payment Recieved",
    }

    def _fetch_deal_stages_for_contacts(self, contact_ids: list) -> dict:
        import logging
        log = logging.getLogger(__name__)
        result = {}
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
        window_start_dt=None,
        window_end_dt=None,
    ) -> list:
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
        start_dt=None,
        end_dt=None,
    ) -> dict:
        import logging
        log = logging.getLogger(__name__)

        new_deals = []

        log.info("Fetching scheduled call records (window: %s → %s)...",
                 start_dt.date() if start_dt else "today",
                 end_dt.date() if end_dt else "today")
        sched_call_records = self._fetch_scheduled_call_records_today(start_dt, end_dt)

        contact_ids = list({
            (c.get("Who_Id") or {}).get("id")
            for c in sched_call_records
            if isinstance(c.get("Who_Id"), dict) and c["Who_Id"].get("id")
        })
        log.info("  → fetching phones for %d contacts...", len(contact_ids))
        contact_phones = self._fetch_contact_phones(contact_ids)

        log.info("Fetching RingCX outbound calls with disposition...")
        ringcx_calls = self._fetch_ringcx_calls_today(start_dt, end_dt)

        ringcx_by_phone = {}
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

        log.info("Checking deal stages for long-missed scheduled calls...")
        all_contact_ids = list({r["id_contact"] for r in sc_results if r.get("id_contact")})
        if all_contact_ids:
            deal_info_map = self._fetch_deal_stages_for_contacts(all_contact_ids)
            for r in sc_results:
                cid = r.get("id_contact")
                if cid and cid in deal_info_map:
                    info  = deal_info_map[cid]
                    stage = info["stage"]
                    owner = info["owner"]
                    if stage and stage != "New Deal":
                        r["deal_stage"] = stage
                    if stage in self.OWNER_VISIBLE_STAGES and owner:
                        r["deal_owner"] = owner
                    if (stage in self.STAGE_MOVED_ON
                            and r.get("status") in ("missed", "late")
                            and (r.get("minutes_overdue") or 0) > 90):
                        r["deal_moved_on"] = True
                        r["status"] = "completed_via_deal"

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

        nd_results.sort(key=lambda d: d.get("created_time") or "", reverse=True)
        sc_results.sort(
            key=lambda d: d.get("scheduled_time") or d.get("created_time") or "",
        )

        def summary(results, mode="new"):
            called = [r for r in results if r["status"] in ("completed", "early", "completed_via_deal", "completed_via_workflow")]
            completed    = [r for r in results if r["status"] == "completed"]
            early        = [r for r in results if r["status"] == "early"]
            via_deal     = [r for r in results if r["status"] == "completed_via_deal"]
            via_workflow = [r for r in results if r["status"] == "completed_via_workflow"]
            on_time      = [r for r in completed if r.get("on_time")]
            missed       = [r for r in results if r["status"] == "missed"]
            s = {
                "total": len(results),
                "completed": len(called),
                "on_time": len(on_time),
                "early": len(early),
                "via_deal": len(via_deal),
                "via_workflow": len(via_workflow),
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
        import logging
        log = logging.getLogger(__name__)

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

        sms_messages = []
        sms_module = "ringcentralbulksmsextensionforzohocrm__RingCentral_SMS_History"
        try:
            resp = requests.get(
                f"{self.base_url}/crm/v6/{sms_module}/search",
                headers=self._headers(),
                params={
                    "criteria": f"(Who_Id:equals:{contact_id})",
                    "per_page": 30,
                    "sort_by": "Created_Time",
                    "sort_order": "desc",
                },
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
