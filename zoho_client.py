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
#   -EARLY_BEFORE_MIN to +ON_TIME_AFTER_MIN  → "completed" on_time=True  (-15 to +10 min)
#   > +ON_TIME_AFTER_MIN → "completed" on_time=False  (late, > 10 min after)
EARLY_BEFORE_MIN     = int(os.getenv("EARLY_BEFORE_MIN", "15"))
ON_TIME_AFTER_MIN    = int(os.getenv("ON_TIME_AFTER_MIN", "10"))
# Earliest hour (local time) at which dialing begins. Calls scheduled before
# this hour are treated as if they were scheduled at this hour for classification.
DIAL_START_HOUR      = int(os.getenv("DIAL_START_HOUR", "9"))
# Legacy: kept only for compatibility with anything still reading SCHEDULED_TOLERANCE_MINUTES
SCHEDULED_CALL_TOLERANCE_MINUTES = ON_TIME_AFTER_MIN
# Local timezone offset from UTC (e.g. -4 for EDT, -5 for EST)
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "-4"))
# DST-aware local business zone. The fixed TZ_OFFSET_HOURS above (-6 on Render) is CST and is
# an hour wrong all summer: a 9:30 AM CDT call read as 8:30 "local", got floored to the 9 AM
# dial start, and everything downstream (overdue detection, on-time scoring, day boundaries)
# shifted by an hour. astimezone(LOCAL_TZ) applies each instant's own offset, so DST just works.
from zoneinfo import ZoneInfo
LOCAL_TZ = ZoneInfo(os.getenv("TZ_NAME", "America/Chicago"))
# Hard ceiling on paginated Zoho searches. A single day never approaches this;
# it exists only so a bad `more_records` response can't spin a `while True`
# forever and wedge the background refresh (which froze the board for hours).
MAX_SEARCH_PAGES = 50

# US timezone abbreviations → UTC offset (July / DST assumed for the 2-letter
# generic forms). Used to parse the free-text Best Contact Time field, e.g.
# "July 7, 2026, 12:30 PM PT".
_BCT_TZ_OFFSETS = {
    "PT": -7, "PST": -8, "PDT": -7,
    "MT": -6, "MST": -7, "MDT": -6,
    "CT": -5, "CST": -6, "CDT": -5,
    "ET": -4, "EST": -5, "EDT": -4,
    "AT": -4, "AST": -4, "ADT": -3,
    "HT": -10, "HST": -10, "AKT": -8, "AKDT": -8, "AKST": -9,
}


def normalize_phone(phone: str) -> Optional[str]:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def _normalize_ig_handle(value: Optional[str]) -> str:
    """Turn a raw Instagram value into '@handle', or '' if empty.

    Accepts bare handles, '@handle', or full instagram.com profile URLs.
    """
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    low = s.lower()
    for marker in ("instagram.com/", "instagr.am/"):
        idx = low.find(marker)
        if idx != -1:
            s = s[idx + len(marker):]
            break
    # Strip leading @, surrounding slashes, then any trailing path/query.
    s = s.strip().lstrip("@").strip("/")
    s = s.split("/")[0].split("?")[0].strip()
    return ("@" + s) if s else ""


def _owner_name(value) -> str:
    if isinstance(value, dict):
        return value.get("name") or value.get("full_name") or ""
    return str(value) if value else ""


try:
    from zoneinfo import ZoneInfo
    _NOTE_TZ = ZoneInfo(os.getenv("NOTE_PARSE_TZ", "America/New_York"))
except Exception:
    _NOTE_TZ = timezone(timedelta(hours=-4))  # fallback to EDT


_FOLLOWUP_KEYWORDS = (
    "follow up", "follow-up", "followup",
    "callback", "call back",
    "will call", "i will call", "call her", "call him",
    "reach out", "circle back",
)

# Trigger words that justify treating a date+time mention as an actual FU plan
# (rather than e.g. someone's date of birth or a procedure date).
_FU_TRIGGERS = ("fu ", "fu:", "fu.", "f/u", "follow up", "follow-up", "followup")

# Numeric date: MM/DD or MM/DD/YY(YY) — also accepts hyphens or dots as separators.
_NOTE_DATE_RX = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?(?!\d)")
# Time: 1:30 p.m., 1:30pm, 1 PM, 13:30
_NOTE_TIME_RX = re.compile(
    r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?(?!\w)",
    re.IGNORECASE,
)

_NO_FOLLOWUP = {
    "status": "forgotten", "when": None, "by": None,
    "summary": None, "kind": None,
}


def _parse_followup_from_note(content: str, now_dt: datetime) -> Optional[tuple]:
    """If the note text mentions a follow-up with an explicit future date+time,
    return (when_dt, snippet); else None.

    A trigger word ("FU", "follow up", etc.) must appear *before* the date —
    avoids capturing date-of-birth or procedure dates the rep may also note.
    """
    if not content:
        return None
    lc = content.lower()
    trigger_idx = -1
    for trig in _FU_TRIGGERS:
        i = lc.find(trig)
        if i >= 0 and (trigger_idx < 0 or i < trigger_idx):
            trigger_idx = i
    if trigger_idx < 0:
        return None

    # Look for the first date pattern AFTER the trigger
    date_m = _NOTE_DATE_RX.search(content, trigger_idx)
    if not date_m:
        return None
    month, day, year = int(date_m.group(1)), int(date_m.group(2)), date_m.group(3)
    if year:
        y = int(year)
        year_n = y + 2000 if y < 100 else y
    else:
        year_n = now_dt.year
    try:
        d = datetime(year_n, month, day)
    except ValueError:
        return None

    # Look for the first time AFTER the date
    hour, minute = 9, 0  # default to 9 AM if no time given
    time_m = _NOTE_TIME_RX.search(content, date_m.end())
    if time_m:
        hour = int(time_m.group(1))
        minute = int(time_m.group(2) or 0)
        ampm = time_m.group(3).lower()
        if ampm == "p" and hour < 12:
            hour += 12
        elif ampm == "a" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None

    when_dt = datetime(year_n, month, day, hour, minute, tzinfo=_NOTE_TZ)
    # If the parsed date is in the past but no year was given, roll forward a year
    if when_dt <= now_dt and not year:
        try:
            when_dt = when_dt.replace(year=year_n + 1)
        except ValueError:
            pass
    if when_dt <= now_dt:
        return None

    # Snippet centered on the trigger word, single-line, HTML stripped
    end = max(date_m.end(), (time_m.end() if time_m else date_m.end()))
    snippet = content[trigger_idx:min(end + 20, len(content))]
    snippet = re.sub(r"<[^>]+>", "", snippet)  # strip rich-text tags
    snippet = " ".join(snippet.split())[:120]
    return when_dt, snippet


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO 8601 parser. Handles:
      - `-0400` and `-04:00` offsets (Python 3.9's fromisoformat needs the colon)
      - date-only strings (YYYY-MM-DD) — interpreted as end-of-day UTC so a task
        whose Due_Date is "today" still tests as future for most of the day.
    """
    if not ts:
        return None
    s = str(ts)
    # Date-only? Treat as end of day UTC.
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.fromisoformat(s)
            return d.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return None
    s = s.replace("Z", "+00:00")
    # Insert colon into 4-digit offset (e.g. -0400 → -04:00)
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None


def _normalize_activity(kind: str, row: dict, after_dt: datetime) -> Optional[dict]:
    """Turn a related-list row into a uniform activity dict, filtering by after_dt."""
    if kind == "Calls":
        ts = row.get("Call_Start_Time") or row.get("Created_Time")
    else:
        ts = row.get("Modified_Time") or row.get("Created_Time") or row.get("Last_Modified_Time")
    ts_dt = _parse_iso(ts)
    if ts_dt is None or ts_dt <= after_dt:
        return None

    if kind == "Notes":
        summary = row.get("Note_Title") or ""
        detail = (row.get("Note_Content") or "")[:400]
        by = _owner_name(row.get("Created_By") or row.get("Owner"))
    elif kind == "Tasks":
        summary = row.get("Subject") or ""
        detail = f"{row.get('Status') or ''} · due {row.get('Due_Date') or '—'}".strip(" ·")
        by = _owner_name(row.get("Owner"))
    elif kind == "Calls":
        summary = row.get("Subject") or ""
        disp = row.get("Outgoing_call_disposition") or ""
        ctype = row.get("Call_Type") or ""
        detail = " · ".join(p for p in (ctype, disp) if p)
        by = _owner_name(row.get("Owner"))
    elif kind == "Stage_History__s":
        old = row.get("Old_Value") or ""
        new = row.get("New_Value") or row.get("Stage") or ""
        summary = f"Stage: {old} → {new}" if old else f"Stage → {new}"
        detail = ""
        by = _owner_name(row.get("Modified_By"))
    else:
        return None

    return {
        "kind": kind.replace("__s", ""),
        "ts": ts_dt.isoformat(),
        "by": by,
        "summary": summary,
        "detail": detail,
        "id": row.get("id"),
    }


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
        """Returns (start_of_today, end_of_today) in UTC, based on local business day."""
        local_now = datetime.now(LOCAL_TZ)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end   = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
        return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)

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
        local_tz = LOCAL_TZ
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
        """Returns (start_str, end_str) covering local today's bounds in UTC."""
        today_start, today_end = self._today_bounds()
        return (
            today_start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
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

    def _fetch_calls_by_contact_ids(
        self,
        contact_ids: list[str],
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> dict[str, list[dict]]:
        """Fetch outbound calls by Who_Id (contact link) using batched COQL.

        COQL `Who_Id in (...)` finds every call linked to a contact — including
        manual logs ("2nd Attempt - No Answer") that have no phone in the
        Subject and would otherwise slip through the Subject-based search.

        Returns {contact_id: [call_records]}.
        """
        import logging
        log = logging.getLogger(__name__)
        result: dict[str, list[dict]] = {}
        if not contact_ids:
            return result

        if start_dt and end_dt:
            start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        else:
            start_str, end_str = self._call_window()

        BATCH = 25
        for i in range(0, len(contact_ids), BATCH):
            batch = contact_ids[i : i + BATCH]
            id_list = ",".join(f"'{cid}'" for cid in batch)
            offset = 0
            while True:
                query = (
                    "select id, Subject, Call_Start_Time, Who_Id, Call_Type, "
                    "Outgoing_call_disposition, Call_Duration_in_seconds, "
                    "Owner, Created_Time, Description "
                    f"from Calls where (Who_Id in ({id_list})) "
                    f"and (Call_Start_Time between '{start_str}' and '{end_str}') "
                    f"order by Call_Start_Time desc limit 200 offset {offset}"
                )
                try:
                    resp = requests.post(
                        f"{self.base_url}/crm/v6/coql",
                        headers=self._headers(),
                        json={"select_query": query},
                        timeout=25,
                    )
                except Exception as e:
                    log.warning("Who_Id COQL batch error: %s", e)
                    break
                if resp.status_code == 204:
                    break
                if not resp.ok:
                    log.warning("Who_Id COQL failed %s: %s",
                                resp.status_code, resp.text[:200])
                    break
                data = resp.json()
                for c in data.get("data", []):
                    subj = (c.get("Subject") or "").lower()
                    if subj.startswith("scheduled call") or subj.startswith("call scheduled"):
                        continue
                    if c.get("Call_Type") != "Outbound":
                        continue
                    who = c.get("Who_Id") or {}
                    cid = who.get("id") if isinstance(who, dict) else None
                    if cid:
                        result.setdefault(cid, []).append(c)
                if not data.get("info", {}).get("more_records"):
                    break
                offset += 200

        total = sum(len(v) for v in result.values())
        log.info("  → %d calls matched via Who_Id for %d/%d contacts",
                 total, len(result), len(contact_ids))
        return result

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
                    subj = (call.get("Subject") or "").lower()
                    if subj.startswith("scheduled call") or subj.startswith("call scheduled"):
                        continue
                    phone = self._phone_from_subject(call.get("Subject", "") or "")
                    if phone and phone in phone_map:
                        phone_map[phone].append(call)
                used_fallback = True
                break
            for call in calls:
                subj = (call.get("Subject") or "").lower()
                if subj.startswith("scheduled call") or subj.startswith("call scheduled"):
                    continue
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
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # Treat a naive timestamp as UTC so downstream tz math (same-day
        # bucketing, offset-from-schedule) never compares naive vs aware or
        # silently assumes the server's local zone.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _caller_name(call: Optional[dict]) -> Optional[str]:
        if not call:
            return None
        owner = call.get("Owner")
        if isinstance(owner, dict):
            return owner.get("name")
        return owner if isinstance(owner, str) else None

    @staticmethod
    def _caller_owner_id(call: Optional[dict]) -> Optional[str]:
        if not call:
            return None
        owner = call.get("Owner")
        if isinstance(owner, dict):
            return owner.get("id")
        return None

    @staticmethod
    def _name_from_sched_subject(subject: Optional[str]) -> Optional[str]:
        """Pull the lead name out of a scheduled-call Subject when there's no
        linked contact. Handles 'Scheduled call — Name', 'Scheduled Call: Name',
        'Call scheduled with Name'."""
        s = (subject or "").strip()
        m = re.match(r"(?i)^(?:scheduled call|call scheduled)\s*(?:[—–:\-]+|with)\s*(.+)$", s)
        name = m.group(1).strip() if m else ""
        return name or None

    @staticmethod
    def _is_outbound_call(c: dict) -> bool:
        """Whether a logged call counts as an outbound dial against a schedule.

        A dispositioned call is outbound. Explicit Call_Type wins next. Falling
        back to the Subject phrasing catches RingEX/MVP synthetic logs and
        common variants ('Outgoing call to', 'Outbound Call to', 'Call to').
        Inbound markers are excluded so an inbound call to the same contact's
        number never gets credited as completing a scheduled outbound call.
        """
        subj = (c.get("Subject") or "").lower().strip()
        # Workflow/status placeholder records are NOT real dials. The Call
        # Cycles automation creates these at the round scheduled-slot time
        # (e.g. 10:00:00) to track attempt counts:
        #   "2nd/3rd/4th Attempt - No Answer", "Scheduled Call: X",
        #   "Call scheduled with X". Exclude them.
        if (subj.startswith("scheduled call") or subj.startswith("call scheduled")
                or re.match(r"^\d+(st|nd|rd|th)\s+attempt\b", subj)):
            return False
        # Never credit an inbound call.
        ctype = (c.get("Call_Type") or "").strip().lower()
        if ctype == "inbound" or "inbound" in subj or "call from" in subj or "incoming" in subj:
            return False
        # RingEX calls (matched to the RingEX platform log) are real dials even
        # though they carry no disposition.
        if c.get("_source") == "ringex":
            return True
        # Everything else is treated as a RingCX (dialer) call: only count it
        # when a disposition was actually recorded. This drops un-dispositioned
        # Zoho records (workflow rows, stubs) that aren't genuine dials.
        return bool(c.get("Outgoing_call_disposition"))

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

    def _classify_scheduled_call(self, rec: dict, calls: list[dict], apply_floor: bool = True) -> dict:
        """Classify a scheduled call against logged RingCX/MVP calls.

        Core principle: actual_call_time is always the RingCX call (with
        disposition) logged in Zoho whose Call_Start_Time is closest to
        the scheduled time.  MVP calls (RingEX, no disposition) contribute
        to dial_attempts and last_attempt_time but never set actual_call_time.

        apply_floor=False skips the pre-9AM clamp — used by "Call Now" deals
        where the target time is the exact deal-created time, not a slot.
        """
        scheduled = self._parse_dt(rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"))
        if not scheduled:
            return {"status": "no_schedule", "dial_attempts": 0, "caller": None}

        # Apply the same pre-9AM floor the frontend uses, so the backend's
        # same-day match, offset, and on-time/late verdict agree with what the
        # board displays (previously the backend used the raw time and the
        # frontend floored to 9AM, so on-time dials could be scored "late").
        effective_scheduled = self._effective_scheduled(scheduled) if apply_floor else scheduled
        now = datetime.now(timezone.utc)

        def mins_from_schedule(c):
            t = self._parse_dt(c.get("Call_Start_Time"))
            return abs((t - effective_scheduled).total_seconds()) / 60 if t else float("inf")

        # A call only "belongs" to a scheduled slot if it happens on the SAME
        # local calendar date as the scheduled time.  This prevents yesterday's
        # 9 PM dial from matching today's 9 AM schedule.
        local_tz = LOCAL_TZ
        sched_local_date = effective_scheduled.astimezone(local_tz).date()
        def same_local_date(c):
            t = self._parse_dt(c.get("Call_Start_Time"))
            if not t:
                return False
            return t.astimezone(local_tz).date() == sched_local_date

        # ── Gather this slot's outbound dials on the scheduled day ─────
        # Any outbound call to this contact on the scheduled local date counts
        # as a pass — whether or not Zoho recorded a disposition or used the
        # exact "Outgoing call to" subject. Dispositioned calls remain the
        # source of truth for the displayed disposition/caller.
        #
        # A dial can't have happened in the future. Some Zoho records (future
        # call-cycle slots, "Nth Attempt" placeholders) carry a Call_Start_Time
        # later than now — exclude them so they don't show as dials.
        def _not_future(c):
            t = self._parse_dt(c.get("Call_Start_Time"))
            return bool(t) and t <= now

        same_day_calls = [
            c for c in calls
            if same_local_date(c) and _not_future(c) and self._is_outbound_call(c)
        ]
        dialed_with_disp = [
            c for c in same_day_calls if c.get("Outgoing_call_disposition")
        ]
        dial_attempts = len(same_day_calls)

        def _call_source(c: dict) -> str:
            # Authoritative: a call is RingEX only if it matched the RingEX
            # Platform API (tagged during merge). Everything else logged in
            # Zoho is a RingCX (Engage Voice dialer) call.
            return "ringex" if c.get("_source") == "ringex" else "ringcx"

        attempts_detail = sorted(
            [
                {
                    "time": c.get("Call_Start_Time"),
                    "rep": self._caller_name(c),
                    "_owner_id": self._caller_owner_id(c),
                    "disposition": c.get("Outgoing_call_disposition"),
                    "source": _call_source(c),
                    "duration": (c.get("Call_Duration_in_seconds")
                                 or c.get("_duration") or 0),
                }
                for c in same_day_calls
                if c.get("Call_Start_Time")
            ],
            key=lambda a: a["time"],
        )

        most_recent_ringcx = (
            max(dialed_with_disp, key=lambda c: c.get("Call_Start_Time") or "")
            if dialed_with_disp else None
        )
        most_recent_any = (
            max(same_day_calls, key=lambda c: c.get("Call_Start_Time") or "")
            if same_day_calls else None
        )

        # ── Prioritized disposition across all of today's attempts ─────────
        # A "contact-made" outcome (anything other than the no-contact set)
        # always wins over No Answer / Voicemail / Answering Machine — even if
        # it happened on a later redial. e.g. dialed at the scheduled time =
        # No Answer, redialed later = Quote Sent → the row shows "Quote Sent".
        # Among meaningful outcomes, take the most recent.
        NO_CONTACT_DISPOSITIONS = {
            "no answer", "voicemail", "voicemail / no answer",
            "no answer / voicemail", "answering machine", "disposition timeout",
            "call dropped / connection issues", "call dropped / audio issues",
        }
        def _is_meaningful_disp(c):
            d = (c.get("Outgoing_call_disposition") or "").strip().lower()
            return bool(d) and d not in NO_CONTACT_DISPOSITIONS
        _meaningful = [c for c in dialed_with_disp if _is_meaningful_disp(c)]
        best_disp_call = (
            max(_meaningful, key=lambda c: c.get("Call_Start_Time") or "")
            if _meaningful else most_recent_ringcx
        )
        best_disposition = (
            best_disp_call.get("Outgoing_call_disposition")
            if best_disp_call else None
        )

        # Most recent attempt of any kind (for last_attempt_time)
        all_with_time = [
            c for c in same_day_calls
            if self._parse_dt(c.get("Call_Start_Time"))
        ]
        last_attempt = (
            max(all_with_time, key=lambda c: c.get("Call_Start_Time"))
            if all_with_time else None
        )

        # ── Find the call closest to scheduled time ─────────────────────
        # The closest outbound dial to the scheduled time is the single source
        # of truth for actual_call_time and timing classification.
        closest_call = (
            min(same_day_calls, key=mins_from_schedule) if same_day_calls else None
        )

        if closest_call:
            call_dt = self._parse_dt(closest_call.get("Call_Start_Time"))
            offset_min = (
                (call_dt - effective_scheduled).total_seconds() / 60
                if call_dt else None
            )
            is_early = offset_min is not None and offset_min < -EARLY_BEFORE_MIN
            on_time  = (offset_min is not None
                        and -EARLY_BEFORE_MIN <= offset_min <= ON_TIME_AFTER_MIN)
            is_mvp = closest_call.get("_source") == "ringex"
            return {
                "status": "early" if is_early else "completed",
                "scheduled_time": rec.get("Call_Start_Time") or rec.get("Call_Scheduled_Date"),
                "actual_call_time": closest_call.get("Call_Start_Time"),
                "offset_minutes": round(offset_min, 1) if offset_min is not None else None,
                "on_time": on_time,
                "dial_attempts": dial_attempts,
                "disposition": best_disposition,
                "caller": self._caller_name(closest_call),
                "_caller_owner_id": self._caller_owner_id(closest_call),
                "recording_url": self._extract_recording_url(
                    (best_disp_call or closest_call).get("Description") or ""
                ),
                "logged_via": "mvp" if is_mvp else "ringcx",
                "mvp_only": is_mvp,
                "last_attempt_time": (last_attempt.get("Call_Start_Time")
                                      if last_attempt else None),
                "recent_attempts": attempts_detail,
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
                "_caller_owner_id": self._caller_owner_id(most_recent_any),
                "recent_attempts": attempts_detail,
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
            "mvp_only": False,
            "disposition": best_disposition,
            "caller": self._caller_name(most_recent_any),
            "_caller_owner_id": self._caller_owner_id(most_recent_any),
            "recording_url": None,
            "logged_via": None,
            "last_attempt_time": (last_attempt.get("Call_Start_Time")
                                  if last_attempt else None),
            "recent_attempts": attempts_detail,
        }

    # ------------------------------------------------ scheduled call records

    # Owners whose calendars feed the Scheduled Call tracker. Each owner is
    # presented as its own sheet in the UI; records are tagged with the real
    # Owner name so the frontend can split them.
    SCHEDULED_CALL_OWNERS = ("Zoho Admin", "Ariel Voskin", "Parizher")
    OWNER_DISPLAY_NAMES = {"Parizher": "Anna Parizher"}

    def _fetch_scheduled_call_records_today(
        self,
        window_start_dt: Optional[datetime] = None,
        window_end_dt: Optional[datetime] = None,
    ) -> list[dict]:
        """Fetch 'Scheduled Call' records within the given UTC window.

        Includes records owned by any user in SCHEDULED_CALL_OWNERS (Zoho Admin
        and Ariel Voskin), so each owner's calendar can be shown as a separate
        sheet. If window_start_dt / window_end_dt are None, defaults to today in
        local time. Uses REST API search so Who_Id comes back as a full
        {id, name} dict.
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
        while page <= MAX_SEARCH_PAGES:
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
                if owner_name in self.SCHEDULED_CALL_OWNERS:
                    all_records.append(call)
            if not data.get("info", {}).get("more_records"):
                break
            page += 1

        log.info("  → %d scheduled call records found (%s, today±)",
                 len(all_records), ", ".join(self.SCHEDULED_CALL_OWNERS))
        return self._dedupe_sched_records(all_records)

    @staticmethod
    def _lookup_id(rec: dict, field: str) -> Optional[str]:
        """Pull the id out of a Zoho lookup field ({id, name}) — None if unset."""
        val = rec.get(field) or {}
        return val.get("id") if isinstance(val, dict) else None

    def _dedupe_sched_records(self, records: list[dict]) -> list[dict]:
        """Collapse duplicate 'Scheduled Call' activity records.

        Zoho holds two subject formats for the same appointment:
          • "Scheduled Call: <Name>"  — Who_Id set (the real record)
          • "Scheduled call — <Name>" — Who_Id null (a phantom, no contact link)
        `Subject:starts_with:Scheduled Call` is case-insensitive, so the search
        returns both, and the phantom renders with no phone and no lead source.

        Records are grouped by the appointment they describe (linked deal +
        start time, falling back to contact or subject when there's no deal).
        Inside a group, a contact-linked record always beats a Who_Id-less one.
        Two DIFFERENT contacts are never collapsed together — a shared package
        deal can legitimately have several people booked at the same minute.
        """
        import logging
        log = logging.getLogger(__name__)

        groups: dict[tuple, list[dict]] = {}
        for rec in records:
            when    = rec.get("Call_Start_Time") or ""
            deal_id = self._lookup_id(rec, "What_Id")
            who_id  = self._lookup_id(rec, "Who_Id")
            if deal_id:
                key = ("deal", deal_id, when)
            elif who_id:
                key = ("contact", who_id, when)
            else:
                subj = self._name_from_sched_subject(rec.get("Subject")) or rec.get("Subject") or ""
                key = ("subject", subj.strip().lower(), when)
            groups.setdefault(key, []).append(rec)

        keep_ids: set[str] = set()
        for recs in groups.values():
            if len(recs) == 1:
                keep_ids.add(recs[0]["id"])
                continue
            # Oldest first so the winner is stable across refreshes.
            recs = sorted(recs, key=lambda r: r.get("Created_Time") or "")
            linked = [r for r in recs if self._lookup_id(r, "Who_Id")]
            if linked:
                seen: set[str] = set()
                for r in linked:
                    cid = self._lookup_id(r, "Who_Id")
                    if cid not in seen:
                        seen.add(cid)
                        keep_ids.add(r["id"])
            else:
                # No contact link anywhere in the group — keep a single row.
                keep_ids.add(recs[0]["id"])

        deduped = [r for r in records if r["id"] in keep_ids]
        dropped = len(records) - len(deduped)
        if dropped:
            log.info("  → dropped %d duplicate scheduled-call record(s); %d remain",
                     dropped, len(deduped))
        return deduped

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

    def _instagram_field_api_name(self) -> Optional[str]:
        """Discover the Contacts field that stores the Instagram handle.

        Scans Contacts field metadata for a field whose api_name or label looks
        like Instagram, so we don't hardcode an org-specific custom-field name.
        Cached for the client's lifetime (including a negative result).
        """
        if getattr(self, "_ig_field_cache", "unset") != "unset":
            return self._ig_field_cache
        self._ig_field_cache = None
        try:
            resp = requests.get(
                f"{self.base_url}/crm/v6/settings/fields",
                headers=self._headers(),
                params={"module": "Contacts"},
                timeout=20,
            )
            if resp.ok:
                for f in resp.json().get("fields", []):
                    api = f.get("api_name") or ""
                    label = f.get("field_label") or ""
                    hay = f"{api} {label}".lower()
                    if "instagram" in hay or hay.split() == ["ig"] or "insta" in hay:
                        self._ig_field_cache = api
                        logging.getLogger(__name__).info(
                            "Instagram field resolved to '%s'", api)
                        break
            else:
                logging.getLogger(__name__).warning(
                    "Instagram field discovery failed %s: %s",
                    resp.status_code, resp.text[:200])
        except Exception as e:
            logging.getLogger(__name__).warning("Instagram field discovery error: %s", e)
        return self._ig_field_cache

    def _fetch_contact_details(self, contact_ids: list[str]) -> dict[str, dict]:
        """Batch-fetch Name + Phone + Lead_Source (+ Instagram handle) for Contact IDs.

        Returns {contact_id: {"name": str, "phone": str|None,
        "lead_source": str, "instagram": str}}. Phone falls back to Mobile when
        the Phone field is empty. The Instagram handle is normalized to '@handle'.
        """
        result: dict[str, dict] = {}
        ig_field = self._instagram_field_api_name()
        fields = "id,Full_Name,First_Name,Last_Name,Phone,Mobile,Lead_Source" + (f",{ig_field}" if ig_field else "")
        BATCH = 100
        for i in range(0, len(contact_ids), BATCH):
            batch = contact_ids[i : i + BATCH]
            resp = requests.get(
                f"{self.base_url}/crm/v6/Contacts",
                headers=self._headers(),
                params={"ids": ",".join(batch), "fields": fields},
                timeout=20,
            )
            if resp.ok:
                for c in resp.json().get("data", []):
                    nm = (c.get("Full_Name") or "").strip()
                    if not nm:
                        nm = f"{c.get('First_Name','') or ''} {c.get('Last_Name','') or ''}".strip()
                    result[c["id"]] = {
                        "name": nm,
                        "phone": (normalize_phone(c.get("Phone") or "")
                                  or normalize_phone(c.get("Mobile") or "")),
                        "lead_source": c.get("Lead_Source") or "",
                        "instagram": _normalize_ig_handle(c.get(ig_field)) if ig_field else "",
                    }
        return result

    # Stages that indicate the deal was successfully closed/handled outside of RingCX
    # — long-overdue scheduled calls in these stages count as completed
    STAGE_MOVED_ON = {
        "Closed Won - Surgery Scheduled",
        "Unsubscribe",
        "Quote Sent",
        "Retainer Invoice Sent",
    }

    # Stages that count as the lead having genuinely progressed, ranked so the
    # most advanced wins. Used to pull the RIGHT deal when a scheduled call is
    # linked to a stale deal (Call Scheduled / Unsubscribe) but the contact has
    # another deal that actually moved forward.
    PROGRESSED_STAGE_RANK = {
        "Quote Sent": 1,
        "Retainer Invoice Sent": 2,
        "Retainer Paid": 3,
        "Closed Won - Surgery Scheduled": 4,
    }

    def _fetch_best_progressed_stage_for_contacts(self, contact_ids: list[str]) -> dict[str, str]:
        """For each contact, return their most-advanced 'progressed' deal stage
        (Quote Sent → Closed Won), scanning ALL their deals. {contact_id: stage}."""
        import logging
        log = logging.getLogger(__name__)
        result: dict[str, str] = {}
        BATCH = 15
        for i in range(0, len(contact_ids), BATCH):
            batch = contact_ids[i : i + BATCH]
            conditions = " or ".join(f"(Contact_Name:equals:{cid})" for cid in batch)
            try:
                resp = requests.get(
                    f"{self.base_url}/crm/v6/Deals/search",
                    headers=self._headers(),
                    params={"criteria": conditions, "fields": "id,Stage,Contact_Name",
                            "per_page": 100},
                    timeout=20,
                )
            except Exception as e:
                log.warning("Progressed-stage lookup error: %s", e)
                continue
            if resp.status_code == 204 or not resp.ok:
                continue
            for deal in resp.json().get("data", []):
                cn = deal.get("Contact_Name")
                cid = cn.get("id") if isinstance(cn, dict) else cn
                stage = deal.get("Stage") or ""
                rank = self.PROGRESSED_STAGE_RANK.get(stage, 0)
                if cid and rank > 0:
                    if self.PROGRESSED_STAGE_RANK.get(result.get(cid, ""), 0) < rank:
                        result[cid] = stage
        if result:
            log.info("  → found a progressed deal for %d of %d checked contacts",
                     len(result), len(contact_ids))
        return result

    PIPELINE_STAGES = [
        "Quote Sent",
        "Retainer Invoice Sent",
        "Closed Won - Surgery Scheduled",
    ]

    def get_pipeline_counts(
        self,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> dict:
        """Returns {stage: [{owner, count}]} for deals at each pipeline stage
        Modified within [start_dt, end_dt]. Defaults to today's local window."""
        log = logging.getLogger(__name__)
        local_tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
        if start_dt is None:
            now_local = datetime.now(local_tz)
            start_dt = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        lookback = start_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        lookback = lookback[:-2] + ":" + lookback[-2:] if lookback[-3] != ":" else lookback

        upper_clause = ""
        if end_dt is not None:
            upper = end_dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            upper = upper[:-2] + ":" + upper[-2:] if upper[-3] != ":" else upper
            upper_clause = f"and(Modified_Time:less_equal:{upper})"

        result = {}
        for stage in self.PIPELINE_STAGES:
            criteria = (
                f"((Stage:equals:{stage})"
                f"and(Modified_Time:greater_equal:{lookback}){upper_clause})"
            )
            all_deals, page = [], 1
            while page <= MAX_SEARCH_PAGES:
                resp = requests.get(
                    f"{self.base_url}/crm/v6/Deals/search",
                    headers=self._headers(),
                    params={
                        "criteria": criteria,
                        "fields": "id,Deal_Name,Owner",
                        "per_page": 200,
                        "page": page,
                    },
                    timeout=20,
                )
                if resp.status_code == 204 or not resp.ok:
                    break
                data = resp.json()
                all_deals.extend(data.get("data", []))
                if not data.get("info", {}).get("more_records"):
                    break
                page += 1

            by_owner = {}
            for d in all_deals:
                owner_info = d.get("Owner") or {}
                if isinstance(owner_info, dict):
                    owner = owner_info.get("name") or "Unassigned"
                else:
                    owner = str(owner_info) if owner_info else "Unassigned"
                by_owner[owner] = by_owner.get(owner, 0) + 1
            sorted_owners = sorted(by_owner.items(), key=lambda x: -x[1])
            result[stage] = {
                "total": len(all_deals),
                "by_agent": [{"name": n, "count": c} for n, c in sorted_owners],
            }
            log.info("Pipeline stage '%s': %d deals", stage, len(all_deals))
        return result

    # Stages where the deal owner's name should be surfaced on the record
    OWNER_VISIBLE_STAGES = {
        "Quote Sent",
        "Retainer Invoice Sent",
        "Closed Won - Surgery Scheduled",
        "Payment Received",
        "Payment Recieved",  # Zoho typo variant
    }

    def _fetch_deal_stages_by_ids(self, deal_ids: list[str]) -> dict[str, dict]:
        """Returns {deal_id: {"stage", "name", "owner", "owner_id", "language",
        "contact_name", "contact_id", "phone", "lead_source"}} for the specific
        deals. contact_name/id come from the deal's Contact_Name lookup so a
        deal-linked scheduled call with no Who_Id can still show the person (not
        the package name); phone/lead_source back-fill the same gap."""
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
                    "fields": "id,Deal_Name,Stage,Owner,Language,Contact_Name,Phone,Lead_Source",
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
                    owner_id = owner.get("id") if isinstance(owner, dict) else None
                    lang_raw = (deal.get("Language") or "").strip()
                    con = deal.get("Contact_Name") or {}
                    result[did] = {
                        "stage": deal.get("Stage") or "",
                        "name": deal.get("Deal_Name") or "",
                        "owner": owner_name,
                        "owner_id": owner_id,
                        "language": lang_raw if lang_raw and lang_raw != "Unselected" else "",
                        "contact_name": (con.get("name") if isinstance(con, dict) else "") or "",
                        "contact_id": (con.get("id") if isinstance(con, dict) else None),
                        "phone": normalize_phone(deal.get("Phone") or ""),
                        "lead_source": deal.get("Lead_Source") or "",
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
        while page <= MAX_SEARCH_PAGES:
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

    # -------------------------------------------------- deal activity (quote panel)

    def get_deal_post_quote_signals(self, deal_id: str, after_iso: str) -> dict:
        """Single CRM round-trip yielding both post-send activities and the
        next-follow-up status for one Deal.

        Returns:
            {
              "activities": [...],   # Notes/Tasks/Calls modified after after_iso
              "next_followup": {
                "status": "scheduled" | "mentioned" | "forgotten",
                "when":   iso,       # nullable
                "by":     str,       # nullable
                "summary": str,      # nullable
                "kind":    "Task"|"Call"|"Note",
              }
            }

        Follow-up rules:
          - scheduled: any open Task with future Due_Date, or any Call with
            future Call_Start_Time (the soonest one wins).
          - mentioned: no scheduled item, but a post-send Note mentions
            "follow up" / "callback" / "will call".
          - forgotten: neither.
        """
        after_dt = _parse_iso(after_iso)
        if after_dt is None:
            return {"activities": [], "next_followup": _NO_FOLLOWUP}

        now_utc = datetime.now(timezone.utc)
        activities: list[dict] = []
        scheduled_candidates: list[tuple] = []  # (when_dt, kind, summary, by, id)
        note_scheduled: Optional[tuple] = None  # (when_dt, snippet, by) — wins over auto-Tasks
        note_mentions_followup = False

        related_specs = [
            ("Notes", "Notes",
             "id,Note_Title,Note_Content,Created_Time,Modified_Time,Owner,Created_By"),
            ("Tasks", "Tasks",
             "id,Subject,Status,Due_Date,Created_Time,Modified_Time,Description,Owner"),
            ("Calls", "Calls",
             "id,Subject,Call_Start_Time,Call_Status,Outgoing_call_disposition,Call_Type,Owner,Created_Time"),
        ]
        for kind, api_name, fields in related_specs:
            try:
                # per_page=20 — we only surface the 5 most recent per kind in
                # the panel and use the rest for next_followup detection; 20
                # is more than enough and meaningfully reduces memory per
                # /api/quotes call (3 kinds × 100 quotes × 30 fewer records).
                resp = requests.get(
                    f"{self.base_url}/crm/v6/Deals/{deal_id}/{api_name}",
                    headers=self._headers(),
                    params={"fields": fields, "per_page": 20,
                            "sort_by": "Modified_Time", "sort_order": "desc"},
                    timeout=15,
                )
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "Activity fetch failed for %s/%s: %s", deal_id, kind, e)
                continue
            if resp.status_code == 204 or not resp.ok:
                continue
            rows = (resp.json() or {}).get("data", []) or []
            for row in rows:
                # 1. Past activity (after the quote was sent)
                norm = _normalize_activity(kind, row, after_dt)
                if norm:
                    activities.append(norm)

                # 2. Future follow-up signal
                if kind == "Tasks":
                    status = (row.get("Status") or "").lower()
                    if status in ("completed", "closed", "cancelled", "canceled"):
                        continue
                    due_dt = _parse_iso(row.get("Due_Date"))
                    if due_dt and due_dt > now_utc:
                        scheduled_candidates.append((
                            due_dt, "Task",
                            row.get("Subject") or "Task",
                            _owner_name(row.get("Owner")),
                            row.get("id"),
                        ))
                elif kind == "Calls":
                    # A future-dated Call record IS the scheduled-callback in Zoho.
                    # Skip calls that already have a disposition (= already happened).
                    if row.get("Outgoing_call_disposition"):
                        continue
                    start_dt = _parse_iso(row.get("Call_Start_Time"))
                    if start_dt and start_dt > now_utc:
                        scheduled_candidates.append((
                            start_dt, "Call",
                            row.get("Subject") or "Scheduled call",
                            _owner_name(row.get("Owner")),
                            row.get("id"),
                        ))
                elif kind == "Notes":
                    # Only consider notes recorded after the quote was sent.
                    created_dt = _parse_iso(row.get("Created_Time"))
                    if not created_dt or created_dt <= after_dt:
                        continue
                    raw_content = ((row.get("Note_Title") or "") + " "
                                    + (row.get("Note_Content") or ""))
                    # Soft signal: ANY mention of follow-up keywords.
                    if not note_mentions_followup and \
                            any(kw in raw_content.lower() for kw in _FOLLOWUP_KEYWORDS):
                        note_mentions_followup = True
                    # Skip AUTO FU log notes — the Call record is the
                    # authoritative source for those; the note text may
                    # contain a UTC timestamp the parser misreads as 9 AM.
                    if raw_content.strip().upper().startswith("AUTO FU:"):
                        continue
                    # Strong signal: explicit FU date + time in the note text.
                    parsed = _parse_followup_from_note(raw_content, now_utc)
                    if parsed and (note_scheduled is None or parsed[0] < note_scheduled[0]):
                        by = _owner_name(row.get("Created_By") or row.get("Owner"))
                        note_scheduled = (parsed[0], parsed[1], by)

        activities.sort(key=lambda a: a.get("ts") or "", reverse=True)

        # Priority: Call/Task records beat note-parsed dates when an AUTO FU
        # call exists (those are machine-created, the note is just a log).
        # Otherwise note-derived dates win (rep's explicit intent).
        auto_fu_call = any(
            kind == "Call" and (summary or "").upper().startswith("AUTO FU:")
            for _, kind, summary, _, _ in scheduled_candidates
        )
        if auto_fu_call and scheduled_candidates:
            when_dt, kind, summary, by, _id = min(scheduled_candidates, key=lambda x: x[0])
            next_followup = {
                "status": "scheduled",
                "when":   when_dt.isoformat(),
                "by":     by,
                "summary": summary,
                "kind":   kind,
                "source": kind.lower(),
            }
        elif note_scheduled:
            when_dt, snippet, by = note_scheduled
            next_followup = {
                "status": "scheduled",
                "when":   when_dt.isoformat(),
                "by":     by,
                "summary": snippet,
                "kind":   "Note",
                "source": "note",
            }
        elif scheduled_candidates:
            when_dt, kind, summary, by, _id = min(scheduled_candidates, key=lambda x: x[0])
            next_followup = {
                "status": "scheduled",
                "when":   when_dt.isoformat(),
                "by":     by,
                "summary": summary,
                "kind":   kind,
                "source": kind.lower(),
            }
        elif note_mentions_followup:
            next_followup = {
                "status": "mentioned", "when": None, "by": None,
                "summary": "Follow-up mentioned in a note",
                "kind": "Note", "source": "note",
            }
        else:
            next_followup = dict(_NO_FOLLOWUP)
            next_followup["source"] = None

        return {"activities": activities, "next_followup": next_followup}

    # Backward-compatible alias — returns just the activities list.
    def get_deal_activity_after(self, deal_id: str, after_iso: str) -> list[dict]:
        return self.get_deal_post_quote_signals(deal_id, after_iso)["activities"]

    # --------------------------------------------------------------- main

    def get_dashboard_data(
        self,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
        supplemental_calls: Optional[dict] = None,
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
        log.info("  → fetching phones + lead source for %d contacts...", len(contact_ids))
        contact_details = self._fetch_contact_details(contact_ids)
        contact_phones = {cid: d["phone"] for cid, d in contact_details.items()}

        # Deals linked directly to a scheduled call (What_Id). Fetched up-front so
        # sched_phone() below can fall back to the deal's Phone for a call that has
        # no Who_Id. Same request count as fetching it later — just reordered.
        linked_deal_ids = list({
            self._lookup_id(c, "What_Id") for c in sched_call_records
            if self._lookup_id(c, "What_Id")
        })
        deal_by_id_map = {}
        if linked_deal_ids:
            log.info("Fetching deal stages by deal ID for %d linked deals...", len(linked_deal_ids))
            deal_by_id_map = self._fetch_deal_stages_by_ids(linked_deal_ids)

        def sched_phone(rec) -> Optional[str]:
            """Best-known phone for a scheduled call: contact first, then deal.

            Display only. Deliberately NOT used to build `all_phones` below: the
            Subject-like COQL search allows just 2 phones per query (BATCH = 2),
            so every extra phone costs another sequential round-trip inside a
            60s refresh loop that already fires ~64 of them on a single worker.
            Feeding deal phones in there would let Who_Id-less rows match their
            own dials, but it starved Render's 5s health check. Revisit once the
            refresh no longer runs in the web process.
            """
            cid = self._lookup_id(rec, "Who_Id")
            phone = contact_phones.get(cid) if cid else None
            if phone:
                return phone
            did = self._lookup_id(rec, "What_Id")
            return (deal_by_id_map.get(did) or {}).get("phone") if did else None

        # Source B: Outbound calls for the phones we care about.
        # Two strategies: (1) Subject-like phone match, (2) Who_Id contact match.
        # Many RingCX dialer calls have subjects like "OUT - Campaign" without
        # the phone number, so Subject-only misses them.
        all_phones = [p for p in contact_phones.values() if p]
        log.info("Fetching outbound calls for %d unique phones...", len(set(all_phones)))
        ringcx_by_phone = self._fetch_all_calls_for_phones(all_phones, start_dt, end_dt)

        # Source B2: Who_Id-based lookup catches calls Subject-matching missed.
        cid_to_phone = {cid: phone for cid, phone in contact_phones.items() if phone}
        calls_by_whoid = self._fetch_calls_by_contact_ids(contact_ids, start_dt, end_dt)
        merged_whoid = 0
        for cid, calls in calls_by_whoid.items():
            phone = cid_to_phone.get(cid)
            if not phone:
                continue
            # Both the phone-Subject search and the Who_Id search return the
            # SAME Zoho Calls, so dedup by the record id (exact identity) —
            # never by time proximity, which would wrongly collapse two
            # distinct dials placed within 2 minutes of each other.
            existing = ringcx_by_phone.setdefault(phone, [])
            existing_ids = {c.get("id") for c in existing if c.get("id")}
            for call in calls:
                cid_call = call.get("id")
                if cid_call and cid_call in existing_ids:
                    continue
                existing.append(call)
                if cid_call:
                    existing_ids.add(cid_call)
                merged_whoid += 1
        if merged_whoid:
            log.info("Merged %d Who_Id-matched calls into phone map", merged_whoid)

        # Source C: RingEX Platform API calls.
        # The RingEX API only knows about calls placed through RingEX (manual
        # rep dials), NOT the RingCX (Engage Voice) dialer campaigns. So a
        # match against this API is the authoritative source signal:
        #   - Zoho call that ALSO appears in the RingEX API  → RingEX (EX)
        #   - Zoho call with NO RingEX match                 → RingCX dialer (CX)
        #   - RingEX call not in Zoho at all                 → RingEX (EX), added
        # Match is by start-time proximity (within 2 min = same call).
        if supplemental_calls:
            merged = 0
            tagged_ex = 0
            for phone, rex_calls in supplemental_calls.items():
                if phone not in ringcx_by_phone:
                    ringcx_by_phone[phone] = []
                existing = ringcx_by_phone[phone]
                existing_times = {}
                for c in existing:
                    t = self._parse_dt(c.get("Call_Start_Time"))
                    if t:
                        existing_times[int(t.timestamp())] = c
                for rc in rex_calls:
                    rc_t = self._parse_dt(rc.get("start_time"))
                    if not rc_t:
                        continue
                    rc_ts = int(rc_t.timestamp())
                    agent = rc.get("agent") or ""
                    match_ts = next((et for et in existing_times
                                     if abs(rc_ts - et) < 120), None)
                    if match_ts is not None:
                        # Same call already in Zoho — this is a RingEX call.
                        # Tag the source and backfill the rep if missing.
                        matched = existing_times[match_ts]
                        matched["_source"] = rc.get("source") or "ringex"
                        if agent and not self._caller_name(matched):
                            matched["Owner"] = {"name": agent}
                        tagged_ex += 1
                        continue
                    new_call = {
                        "Call_Start_Time": rc.get("start_time"),
                        "Subject": f"Outgoing call to {rc.get('to_number', phone)}",
                        "Outgoing_call_disposition": None,
                        "Owner": {"name": agent} if agent else None,
                        "Description": "",
                        "_source": rc.get("source") or "ringex",
                        "_duration": rc.get("duration", 0),
                        "_result": rc.get("result", ""),
                    }
                    existing.append(new_call)
                    existing_times[rc_ts] = new_call
                    merged += 1
            if merged or tagged_ex:
                log.info("RingEX: added %d new, tagged %d existing as EX", merged, tagged_ex)

        nd_phone_to_calls = {}

        def nd_calls_for(deal):
            n = normalize_phone(deal.get("Phone"))
            return nd_phone_to_calls.get(n, []) if n else []

        def sc_calls_for(sched_record):
            # Contact phone only — `ringcx_by_phone` is keyed off `all_phones`,
            # which deliberately excludes deal phones (see sched_phone).
            cid = self._lookup_id(sched_record, "Who_Id")
            phone = contact_phones.get(cid) if cid else None
            by_phone = ringcx_by_phone.get(phone, []) if phone else []
            by_cid = calls_by_whoid.get(cid, []) if cid else []
            if not by_cid:
                return by_phone
            if not by_phone:
                return by_cid
            # Dedup by Zoho record id (both lists are Zoho Calls). RingEX
            # API-added calls have no id and live only in by_phone, so they
            # pass through untouched.
            merged = list(by_phone)
            seen_ids = {c.get("id") for c in merged if c.get("id")}
            for c in by_cid:
                cid_call = c.get("id")
                if cid_call and cid_call in seen_ids:
                    continue
                merged.append(c)
                if cid_call:
                    seen_ids.add(cid_call)
            return merged

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
            cd = contact_details.get(cid) or {}
            # Contact.Phone → Contact.Mobile → linked Deal.Phone. Calls created
            # without a Who_Id have no contact at all, so the deal is the only source.
            phone = sched_phone(rec)
            lead_source = cd.get("lead_source", "")
            # For Facebook / Instagram leads, surface the contact's Instagram
            # handle (as @handle) alongside the lead source on the board.
            instagram = ""
            if lead_source.strip().lower() in ("facebook", "instagram"):
                instagram = cd.get("instagram", "")
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

            owner_obj = rec.get("Owner") or {}
            raw_owner = (
                owner_obj.get("name") if isinstance(owner_obj, dict)
                else owner_obj if isinstance(owner_obj, str)
                else None
            ) or "Zoho Admin"
            owner_name = self.OWNER_DISPLAY_NAMES.get(raw_owner, raw_owner)

            # Name: the linked contact's name — from the Who_Id lookup, then the
            # contact record itself (the lookup often returns a null name even
            # when the contact id is set). If neither, parse the Subject, but
            # mark it PROVISIONAL: subjects like "Scheduled Call: FlexSculpt:
            # Arms" carry a package title, not a person, so the deal's linked
            # Contact_Name should override it in the post-pass. Never fall back
            # to the deal name (a package/procedure title).
            contact_name = (who.get("name") if isinstance(who, dict) else None) or cd.get("name")
            if contact_name:
                name = contact_name
                name_provisional = False
            else:
                name = self._name_from_sched_subject(rec.get("Subject")) or ""
                name_provisional = True

            return {
                "id": rec["id"],
                "id_contact": cid,
                "id_deal": what_id,  # deal linked directly to this call
                "name": name,
                "_name_provisional": name_provisional,
                "phone": phone,
                "lead_source": lead_source,
                "instagram": instagram,
                "created_time": rec.get("Call_Start_Time"),
                "record_created": rec.get("Created_Time"),
                "record_modified": rec.get("Modified_Time"),
                "last_minute": last_minute,
                "owner": owner_name,
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

        # deal_by_id_map was fetched up-front (it feeds phone resolution above).
        # Stages come from the call's direct deal link (What_Id) only — never from
        # the contact's most-recent deal, which is wrong when a contact has several.

        # Build a user ID → name map from deal owners (REST returns full names)
        _user_id_to_name = {}
        for dinfo in deal_by_id_map.values():
            if dinfo.get("owner") and dinfo.get("owner_id"):
                _user_id_to_name[str(dinfo["owner_id"])] = dinfo["owner"]

        # First pass: collect every unresolved owner ID — both the record-level
        # caller and each individual dial attempt's rep. COQL returns Owner as
        # {id} without the name, so these must be resolved to display names.
        unresolved_ids = set()
        for r in sc_results:
            if not r.get("caller"):
                cid = r.get("_caller_owner_id")
                if cid and str(cid) not in _user_id_to_name:
                    unresolved_ids.add(str(cid))
            for a in r.get("recent_attempts") or []:
                if not a.get("rep"):
                    aid = a.get("_owner_id")
                    if aid and str(aid) not in _user_id_to_name:
                        unresolved_ids.add(str(aid))

        # Resolve missing user IDs. A Calls search by Owner is the most reliable
        # (the agent always owns the call we're looking at); fall back to Deals.
        if unresolved_ids:
            for uid in list(unresolved_ids):
                name = None
                for module in ("Calls", "Deals"):
                    try:
                        resp = requests.get(
                            f"{self.base_url}/crm/v6/{module}/search",
                            headers=self._headers(),
                            params={"criteria": f"(Owner:equals:{uid})",
                                    "fields": "Owner", "per_page": 1},
                            timeout=10,
                        )
                        if resp.ok:
                            rows = resp.json().get("data", [])
                            if rows:
                                owner = rows[0].get("Owner")
                                if isinstance(owner, dict) and owner.get("name"):
                                    name = owner["name"]
                                    break
                    except Exception:
                        pass
                if name:
                    _user_id_to_name[uid] = name
            resolved_now = unresolved_ids & set(_user_id_to_name.keys())
            still_missing = unresolved_ids - resolved_now
            if resolved_now:
                log.info("Resolved %d user IDs (caller + attempts)", len(resolved_now))
            if still_missing:
                log.warning("Could not resolve %d owner IDs: %s",
                            len(still_missing), still_missing)

        # Second pass: apply resolved names to record callers and attempt reps
        for r in sc_results:
            if not r.get("caller"):
                caller_id = r.pop("_caller_owner_id", None)
                if caller_id and str(caller_id) in _user_id_to_name:
                    r["caller"] = _user_id_to_name[str(caller_id)]
            else:
                r.pop("_caller_owner_id", None)
            for a in r.get("recent_attempts") or []:
                aid = a.pop("_owner_id", None)
                if not a.get("rep") and aid and str(aid) in _user_id_to_name:
                    a["rep"] = _user_id_to_name[str(aid)]

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
                # Always surface the deal owner for pipeline attribution
                if owner:
                    r["deal_owner"] = owner
                if info.get("name"):
                    r["deal_name"] = info["name"]
                # If the name is provisional (no Who_Id contact — parsed from the
                # subject, which may be a package title), adopt the linked deal's
                # Contact_Name instead. Shows the person and wires up the CRM
                # link + AI summary (id_contact).
                if r.get("_name_provisional") and info.get("contact_name"):
                    r["name"] = info["contact_name"]
                    if not r.get("id_contact") and info.get("contact_id"):
                        r["id_contact"] = info["contact_id"]
                # A call with no Who_Id has no contact to read a lead source from
                # (the phone is already resolved via the deal in sched_base).
                if not r.get("lead_source") and info.get("lead_source"):
                    r["lead_source"] = info["lead_source"]
                # Language from deal record
                if info.get("language"):
                    r["language"] = info["language"]
                # Flag that the deal has moved on, but don't change call status —
                # status should reflect whether a call was actually made.
                if (stage
                        and stage not in ("New Deal", "Call Scheduled")
                        and r.get("status") in ("missed", "late")):
                    r["deal_moved_on"] = True

            # Provisional flag is internal — never leak it to the client.
            r.pop("_name_provisional", None)

        # ── Cross-deal stage correction ────────────────────────────────────
        # A scheduled call is linked to ONE deal (What_Id), which may be stale
        # (still "Call Scheduled" or "Unsubscribe") even though the rep quoted
        # the patient on a redial. When the call disposition signals progress
        # but the linked deal doesn't reflect it, look at the CONTACT's other
        # deals and surface the most-advanced progressed stage instead.
        _NON_PROGRESSED = {"", "New Deal", "Call Scheduled", "Unsubscribe"}
        def _disp_suggests_progress(d):
            d = (d or "").lower()
            return any(k in d for k in ("quote", "retainer", "closed", "sold", "deposit", "paid", "booked"))
        _need_stage = list({
            r["id_contact"] for r in sc_results
            if r.get("id_contact")
            and (r.get("deal_stage") or "") in _NON_PROGRESSED
            and _disp_suggests_progress(r.get("disposition"))
        })
        if _need_stage:
            log.info("Cross-deal stage check for %d contacts (quote-ish disp, stale linked deal)", len(_need_stage))
            _best_stage = self._fetch_best_progressed_stage_for_contacts(_need_stage)
            for r in sc_results:
                cid = r.get("id_contact")
                if cid and cid in _best_stage:
                    r["deal_stage"] = _best_stage[cid]
                    r["_stage_from_other_deal"] = True

        # Workflow auto-completion is intentionally NOT applied. A Zoho workflow
        # marking an overdue call "Completed" (no dial logged) used to hide it
        # behind a "Workflow" status — but those calls still need a real dial, so
        # per the team they must stay visible as Overdue and be distributed by the
        # back-office bot rather than silently disappear.

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

    # ───────────────────────── Call Now deals ─────────────────────────────────

    def _parse_best_contact_time(self, s):
        """Parse the free-text Best Contact Time field, e.g.
        'July 7, 2026, 12:30 PM PT' or 'July 8, 2026, 12:00 PM ET'. Falls back
        to ISO parsing. Returns a tz-aware datetime, or None if unparseable."""
        if not s or not str(s).strip():
            return None
        s = str(s).strip()
        iso = self._parse_dt(s)
        if iso:
            return iso
        m = re.match(
            r"([A-Za-z]+\.?\s+\d{1,2},\s*\d{4}),?\s+(\d{1,2}:\d{2})\s*([AaPp][Mm])\s*([A-Za-z]{2,4})?",
            s,
        )
        if not m:
            return None
        date_part = m.group(1).replace(".", "")
        hm, ap, tz = m.group(2), m.group(3).upper(), (m.group(4) or "").upper()
        for fmt in ("%B %d, %Y %I:%M%p", "%b %d, %Y %I:%M%p"):
            try:
                dt = datetime.strptime(f"{date_part} {hm}{ap}", fmt)
                off = _BCT_TZ_OFFSETS.get(tz, TZ_OFFSET_HOURS)
                return dt.replace(tzinfo=timezone(timedelta(hours=off)))
            except Exception:
                continue
        return None

    def get_call_now_deals(self, start_dt=None, end_dt=None, supplemental_calls=None) -> dict:
        """Deals created in the window whose Best_Contact_Time is empty or set
        BEFORE the deal was created — these should be called ASAP after the deal
        lands. Records are shaped like scheduled calls, with 'scheduled_time' =
        the deal's Created_Time (no 9AM floor). Returns {"records": [...]}."""
        import logging
        log = logging.getLogger(__name__)
        if start_dt and end_dt:
            start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        else:
            start_str, end_str = self._call_window()

        # 1. Deals created in the window.
        deals, offset = [], 0
        while offset <= 2000:
            query = (
                "select id, Deal_Name, Contact_Name, Phone, Stage, Created_Time, "
                "Best_Contact_Time, Lead_Source, Timezone, Owner from Deals "
                f"where Created_Time between '{start_str}' and '{end_str}' "
                f"order by Created_Time desc limit 200 offset {offset}"
            )
            try:
                resp = requests.post(f"{self.base_url}/crm/v6/coql",
                                     headers=self._headers(),
                                     json={"select_query": query}, timeout=25)
            except Exception as e:
                log.warning("Call-now deals fetch error: %s", e); break
            if resp.status_code == 204:
                break
            if not resp.ok:
                log.warning("Call-now deals COQL failed %s: %s", resp.status_code, resp.text[:200]); break
            data = resp.json()
            deals.extend(data.get("data", []))
            if not data.get("info", {}).get("more_records"):
                break
            offset += 200

        # 2. Filter: Best_Contact_Time EMPTY, or set to a time BEFORE the deal
        # was created. A deal with a valid future best-contact-time is NOT a
        # Call Now (they have a real preferred time). Best_Contact_Time is a
        # free-text field ("July 7, 2026, 12:30 PM PT"), so parse it properly.
        call_now = []
        for d in deals:
            created = self._parse_dt(d.get("Created_Time"))
            raw = d.get("Best_Contact_Time")
            raw_str = str(raw).strip() if raw else ""
            is_empty = not raw_str
            # Some deals carry the literal "CALL NOW" / "ASAP" as the best time.
            is_literal = raw_str.upper().replace(" ", "") in ("CALLNOW", "ASAP", "NOW")
            bct = self._parse_best_contact_time(raw)
            if is_empty or is_literal or (bct and created and bct < created):
                call_now.append(d)
        log.info("Call-now deals: %d of %d deals created in window", len(call_now), len(deals))
        if not call_now:
            return {"records": []}

        # 3. Contacts + phones.
        contact_ids = list({
            (d.get("Contact_Name") or {}).get("id")
            for d in call_now
            if isinstance(d.get("Contact_Name"), dict) and d["Contact_Name"].get("id")
        })
        contact_details = self._fetch_contact_details(contact_ids)
        contact_phones = {cid: cd.get("phone") for cid, cd in contact_details.items()}

        # 4. Calls (Subject-phone + Who_Id + RingEX supplemental), deduped by id.
        all_phones = [p for p in contact_phones.values() if p]
        ringcx_by_phone = self._fetch_all_calls_for_phones(all_phones, start_dt, end_dt)
        calls_by_whoid = self._fetch_calls_by_contact_ids(contact_ids, start_dt, end_dt)
        for cid, calls in calls_by_whoid.items():
            phone = contact_phones.get(cid)
            if not phone:
                continue
            existing = ringcx_by_phone.setdefault(phone, [])
            seen = {c.get("id") for c in existing if c.get("id")}
            for c in calls:
                if c.get("id") and c.get("id") not in seen:
                    existing.append(c); seen.add(c.get("id"))
        if supplemental_calls:
            for phone, rex in supplemental_calls.items():
                existing = ringcx_by_phone.setdefault(phone, [])
                ex_ts = {}
                for c in existing:
                    t = self._parse_dt(c.get("Call_Start_Time"))
                    if t:
                        ex_ts[int(t.timestamp())] = c
                for rc in rex:
                    rt = self._parse_dt(rc.get("start_time"))
                    if not rt:
                        continue
                    ts = int(rt.timestamp()); agent = rc.get("agent") or ""
                    m = next((et for et in ex_ts if abs(ts - et) < 120), None)
                    if m is not None:
                        mc = ex_ts[m]; mc["_source"] = rc.get("source") or "ringex"
                        if agent and not self._caller_name(mc):
                            mc["Owner"] = {"name": agent}
                        continue
                    nc = {"Call_Start_Time": rc.get("start_time"),
                          "Subject": f"Outgoing call to {rc.get('to_number', phone)}",
                          "Outgoing_call_disposition": None,
                          "Owner": {"name": agent} if agent else None,
                          "Description": "", "_source": rc.get("source") or "ringex",
                          "_duration": rc.get("duration", 0)}
                    existing.append(nc); ex_ts[ts] = nc

        def calls_for(cid, phone):
            by_phone = ringcx_by_phone.get(phone, []) if phone else []
            by_cid = calls_by_whoid.get(cid, []) if cid else []
            if not by_cid:
                return by_phone
            if not by_phone:
                return by_cid
            merged = list(by_phone); seen = {c.get("id") for c in merged if c.get("id")}
            for c in by_cid:
                if c.get("id") and c.get("id") in seen:
                    continue
                merged.append(c)
                if c.get("id"):
                    seen.add(c.get("id"))
            return merged

        # 5. Build records — classify against the deal's created time (no floor).
        results = []
        need_name = []  # (result_index, phone) for deals with no contact name yet
        for d in call_now:
            con = d.get("Contact_Name") or {}
            cid = con.get("id") if isinstance(con, dict) else None
            cd = contact_details.get(cid) or {}
            phone = cd.get("phone") or normalize_phone(d.get("Phone") or "")
            # Contact's name — from the linked contact record, then the deal's
            # Contact_Name lookup. Never fall back to the Deal_Name (a package/
            # procedure name, not a person).
            name = cd.get("name") or (con.get("name") if isinstance(con, dict) else None) or ""
            # Lead source — the deal's own field first, then the contact record.
            lead_source = (d.get("Lead_Source") or "").strip() or cd.get("lead_source", "")
            synthetic = {
                "id": d["id"],
                "Call_Start_Time": d.get("Created_Time"),
                "Who_Id": {"id": cid, "name": name} if cid else None,
                "Subject": d.get("Deal_Name") or "",
            }
            cls = self._classify_scheduled_call(synthetic, calls_for(cid, phone), apply_floor=False)
            rec = {
                "id": d["id"], "id_contact": cid, "id_deal": d["id"],
                "name": name, "phone": phone,
                "lead_source": lead_source,
                "timezone": (d.get("Timezone") or "").strip(),
                "deal_stage": d.get("Stage") or "",
                "deal_name": d.get("Deal_Name") or "",
                "created_time": d.get("Created_Time"),
                "scheduled_time": d.get("Created_Time"),
                "best_contact_time": d.get("Best_Contact_Time"),
                **cls,
            }
            if not name and phone:
                need_name.append((len(results), phone))
            results.append(rec)

        # 5b. Resolve names for deals with no linked contact via a phone lookup,
        # so the Contact column shows a person — never the package/deal name.
        for idx, phone in need_name[:25]:
            try:
                matches = self.search_contacts(phone)
            except Exception:
                matches = []
            nm = next((m.get("name") for m in matches if m.get("name")), "")
            results[idx]["name"] = nm or "—"

        # 6. Resolve rep/caller owner IDs to names (same as the scheduled path).
        unresolved = set()
        for r in results:
            if not r.get("caller") and r.get("_caller_owner_id"):
                unresolved.add(str(r["_caller_owner_id"]))
            for a in r.get("recent_attempts") or []:
                if not a.get("rep") and a.get("_owner_id"):
                    unresolved.add(str(a["_owner_id"]))
        id_to_name = {}
        for uid in unresolved:
            for module in ("Calls", "Deals"):
                try:
                    resp = requests.get(f"{self.base_url}/crm/v6/{module}/search",
                                        headers=self._headers(),
                                        params={"criteria": f"(Owner:equals:{uid})",
                                                "fields": "Owner", "per_page": 1}, timeout=10)
                    if resp.ok:
                        rows = resp.json().get("data", [])
                        o = rows[0].get("Owner") if rows else None
                        if isinstance(o, dict) and o.get("name"):
                            id_to_name[uid] = o["name"]; break
                except Exception:
                    pass
        for r in results:
            if not r.get("caller"):
                oid = r.pop("_caller_owner_id", None)
                if oid and str(oid) in id_to_name:
                    r["caller"] = id_to_name[str(oid)]
            else:
                r.pop("_caller_owner_id", None)
            for a in r.get("recent_attempts") or []:
                oid = a.pop("_owner_id", None)
                if not a.get("rep") and oid and str(oid) in id_to_name:
                    a["rep"] = id_to_name[str(oid)]

        results.sort(key=lambda r: r.get("created_time") or "", reverse=True)
        return {"records": results}

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
        seen_call_ids = set()

        def _add_call(c):
            cid_call = c.get("id")
            if cid_call and cid_call in seen_call_ids:
                return
            if cid_call:
                seen_call_ids.add(cid_call)
            subj = (c.get("Subject") or "")
            owner = c.get("Owner") or {}
            calls.append({
                "subject": subj, "time": c.get("Call_Start_Time"),
                "type": c.get("Call_Type"),
                "duration_sec": c.get("Call_Duration_in_seconds"),
                "disposition": c.get("Outgoing_call_disposition"),
                "description": (c.get("Description") or "")[:300],
                "owner": owner.get("name") if isinstance(owner, dict) else owner,
            })

        if resp.ok and resp.status_code != 204:
            for c in resp.json().get("data", []):
                _add_call(c)

        # 2b. Also match calls by the contact's phone number. Many RingCX dialer
        # calls are logged to the number with Who_Id=null (not linked to the
        # contact record), so a Who_Id-only search misses them — which made the
        # AI analysis show "no calls" while the board's Dials column (which
        # matches by phone) correctly showed them. Match by Subject phone here
        # so both views agree.
        phone10 = normalize_phone(contact.get("phone") or "")
        if phone10:
            try:
                resp2 = requests.post(
                    f"{self.base_url}/crm/v6/coql",
                    headers=self._headers(),
                    json={"select_query": (
                        "select id, Subject, Call_Start_Time, Call_Type, "
                        "Call_Duration_in_seconds, Outgoing_call_disposition, "
                        "Description, Owner from Calls "
                        f"where Subject like '%{phone10}%' "
                        "order by Call_Start_Time desc limit 100"
                    )},
                    timeout=20,
                )
                if resp2.ok and resp2.status_code != 204:
                    for c in resp2.json().get("data", []):
                        _add_call(c)
            except Exception as e:
                log.warning("Contact summary phone-match failed: %s", e)
            # Keep newest first for the AI prompt / recent-attempts display.
            calls.sort(key=lambda c: c.get("time") or "", reverse=True)

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
            "Outgoing_Call_Status": "Scheduled",
            "Call_Start_Time": call_time,
            "$se_module": "Deals" if deal_id else "Contacts",
            "Who_Id": contact_id,
        }
        if deal_id:
            call_data["What_Id"] = deal_id
        if owner_id:
            call_data["Owner"] = {"id": owner_id}

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

    def update_scheduled_call(
        self,
        call_id: str,
        call_time: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> dict:
        """Reschedule and/or reassign an existing scheduled Call record.

        Args:
            call_id: Zoho Call record ID
            call_time: new ISO 8601 datetime (Call_Start_Time), or None to keep
            owner_id: new Call Owner user ID, or None to keep
        Uses trigger:[] so workflow rules don't fire side effects on the edit.
        """
        data: dict = {"id": call_id}
        if call_time:
            data["Call_Start_Time"] = call_time
        if owner_id:
            data["Owner"] = {"id": owner_id}
        if len(data) == 1:
            return {"id": call_id, "status": "noop"}
        resp = requests.put(
            f"{self.base_url}/crm/v6/Calls",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"data": [data], "trigger": []},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json().get("data", [{}])[0]
        if result.get("status") != "success":
            raise RuntimeError(f"Call update failed: {result.get('message', 'unknown error')}")
        return {"id": call_id, "status": "updated"}

    def delete_scheduled_call(self, call_id: str) -> dict:
        """Delete a scheduled Call record (moves to Zoho Recycle Bin)."""
        resp = requests.delete(
            f"{self.base_url}/crm/v6/Calls/{call_id}",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json().get("data", [{}])[0]
        if result.get("status") != "success":
            raise RuntimeError(f"Call delete failed: {result.get('message', 'unknown error')}")
        return {"id": call_id, "status": "deleted"}

    def get_deal_contact(self, deal_id: str) -> dict:
        """Return contact_id, contact_name, owner_id, owner_name for a Deal."""
        log = logging.getLogger(__name__)
        resp = requests.get(
            f"{self.base_url}/crm/v6/Deals/{deal_id}",
            headers=self._headers(),
            params={"fields": "id,Contact_Name,Owner,Deal_Name"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("get_deal_contact %s → %s", deal_id, resp.status_code)
            return {}
        rows = resp.json().get("data") or []
        if not rows:
            return {}
        data = rows[0]
        contact = data.get("Contact_Name") or {}
        owner = data.get("Owner") or {}
        return {
            "contact_id": contact.get("id", "") if isinstance(contact, dict) else "",
            "contact_name": contact.get("name", "") if isinstance(contact, dict) else str(contact),
            "owner_id": owner.get("id", "") if isinstance(owner, dict) else "",
            "owner_name": owner.get("name", "") if isinstance(owner, dict) else str(owner),
            "deal_name": data.get("Deal_Name", ""),
        }

    def get_scheduled_followup_calls(self, start_iso: str, end_iso: str) -> list[dict]:
        """Return Zoho CRM Call records that look scheduled (no disposition yet) in [start_iso, end_iso].

        This org's Calls module doesn't expose Call_Status in COQL, so
        "scheduled" = call with no Outgoing_call_disposition logged yet.
        """
        log = logging.getLogger(__name__)
        query = (
            "select id, Subject, Call_Start_Time, Call_Type, Who_Id, What_Id, "
            "Owner, Description, Call_Duration_in_seconds, "
            "Outgoing_call_disposition "
            "from Calls "
            f"where Call_Start_Time between '{start_iso}' and '{end_iso}' "
            "and id is not null "
            "order by Call_Start_Time asc limit 200 offset 0"
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
            log.warning("get_scheduled_followup_calls error %s: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json().get("data") or []
        out = []
        for c in data:
            # No disposition = call hasn't been worked yet (= scheduled).
            if c.get("Outgoing_call_disposition"):
                continue
            who = c.get("Who_Id") or {}
            what = c.get("What_Id") or {}
            owner = c.get("Owner") or {}
            out.append({
                "id": c.get("id"),
                "subject": c.get("Subject") or "",
                "call_time": c.get("Call_Start_Time"),
                "status": "Scheduled",
                "contact_id": who.get("id", "") if isinstance(who, dict) else "",
                "contact_name": who.get("name", "") if isinstance(who, dict) else "",
                "deal_id": what.get("id", "") if isinstance(what, dict) else "",
                "deal_name": what.get("name", "") if isinstance(what, dict) else "",
                "owner_id": owner.get("id", "") if isinstance(owner, dict) else "",
                "owner_name": owner.get("name", "") if isinstance(owner, dict) else "",
                "notes": c.get("Description") or "",
                "duration": c.get("Call_Duration_in_seconds") or "",
            })
        return out

    def get_followup_activities(self, start_iso: str, end_iso: str,
                                  limit: int = 500) -> list[dict]:
        """Return Zoho CRM Call records of ANY status within [start_iso, end_iso].

        Powers the Follow Up Activity Tracker — team-lead view of every
        agent's scheduled/completed/cancelled call activity per day. Reads
        Owner so each row carries the agent assignment.
        """
        log = logging.getLogger(__name__)
        limit = max(1, min(limit, 1000))
        # Same lowercase + `between` + `id is not null` pattern as the
        # working Calls queries elsewhere in this file.
        # Call_Status isn't a selectable COQL column on this org's Calls
        # module. Derive status client-side from Outgoing_call_disposition.
        query = (
            "select id, Subject, Call_Start_Time, Call_Type, "
            "Who_Id, What_Id, Owner, Description, "
            "Call_Duration_in_seconds, Created_Time, Modified_Time, "
            "Outgoing_call_disposition "
            "from Calls "
            f"where Call_Start_Time between '{start_iso}' and '{end_iso}' "
            "and id is not null "
            f"order by Call_Start_Time asc limit {limit} offset 0"
        )
        try:
            resp = requests.post(
                f"{self.base_url}/crm/v6/coql",
                headers=self._headers(),
                json={"select_query": query},
                timeout=30,
            )
        except Exception as e:
            log.warning("get_followup_activities request error: %s", e)
            return []
        if resp.status_code == 204:
            return []
        if not resp.ok:
            log.warning("get_followup_activities error %s: %s",
                        resp.status_code, resp.text[:200])
            return []
        data = resp.json().get("data") or []
        # COQL returns lookup fields as id-only (no .name) — resolve owner
        # names in one batch via the cached CRM owners list.
        owner_ids = {(c.get("Owner") or {}).get("id") for c in data}
        owner_ids.discard(None)
        id_to_name: dict = {}
        if owner_ids:
            try:
                for o in self.get_crm_owners():
                    id_to_name[str(o.get("id"))] = o.get("name") or ""
            except Exception as e:
                log.warning("owner lookup failed: %s", e)
        out = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for c in data:
            who = c.get("Who_Id") or {}
            what = c.get("What_Id") or {}
            owner = c.get("Owner") or {}
            disp = c.get("Outgoing_call_disposition") or ""
            # Disposition present = call was worked. No disposition + future
            # start = upcoming on the agent's calendar. No disposition + past
            # start = the agent missed it.
            future = (c.get("Call_Start_Time") or "") > now_iso
            if disp:
                effective = "Completed"
            elif future:
                effective = "Scheduled"
            else:
                effective = "Missed"
            owner_id = owner.get("id", "") if isinstance(owner, dict) else ""
            out.append({
                "id": c.get("id"),
                "subject": c.get("Subject") or "",
                "call_time": c.get("Call_Start_Time"),
                "call_type": c.get("Call_Type") or "",
                "status": effective,
                "disposition": disp,
                "contact_id": who.get("id", "") if isinstance(who, dict) else "",
                "contact_name": who.get("name", "") if isinstance(who, dict) else "",
                "deal_id": what.get("id", "") if isinstance(what, dict) else "",
                "deal_name": what.get("name", "") if isinstance(what, dict) else "",
                "owner_id": owner_id,
                "owner_name": id_to_name.get(str(owner_id), ""),
                "notes": (c.get("Description") or "")[:400],
                "duration": c.get("Call_Duration_in_seconds") or "",
                "created_time": c.get("Created_Time"),
                "modified_time": c.get("Modified_Time"),
            })
        return out

