"""
RingCX / RingCentral live monitoring client.

Two-layer integration:
  1. RingEX  (standard RingCentral Platform API) — agent presence & telephony status
  2. RingCX  (Engage Voice API) — real-time active calls with full detail

Auth flow for RingCX:
  Step 1: JWT → RingCentral access token  (same as RingEX)
  Step 2: Exchange RC token → RingCX token via /api/auth/login/rc/accesstoken

Required env vars:
  RC_CLIENT_ID       – OAuth app client ID
  RC_CLIENT_SECRET   – OAuth app client secret
  RC_JWT_TOKEN       – JWT credential for server-to-server auth
  RC_ACCOUNT_ID      – RingCentral account ID (default: "~" for current)
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


class RingCXClient:
    def __init__(self):
        self.client_id = os.getenv("RC_CLIENT_ID", "")
        self.client_secret = os.getenv("RC_CLIENT_SECRET", "")
        self.jwt_token = os.getenv("RC_JWT_TOKEN", "")
        self.account_id = os.getenv("RC_ACCOUNT_ID", "~")
        self.server_url = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com")
        self.ringcx_url = "https://ringcx.ringcentral.com"
        self.ringcx_api_token = os.getenv("RINGCX_API_TOKEN", "")

        # RingEX token (standard RC platform)
        self._rc_token: Optional[str] = None
        self._rc_token_expiry: float = 0

        # RingCX token (Engage Voice)
        self._cx_token: Optional[str] = None
        self._cx_refresh_token: Optional[str] = None
        self._cx_token_expiry: float = 0
        self._cx_account_id: Optional[str] = None

        # Agent status cache (avoid 62 API calls every 15s)
        self._agents_cache: list[dict] = []
        self._agents_cache_expiry: float = 0
        self._AGENTS_CACHE_TTL = 60  # refresh agents every 60s, not every poll


    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.jwt_token)

    # ══════════════════════════════════════════════════════════════
    # Auth — RingEX (standard RingCentral)
    # ══════════════════════════════════════════════════════════════

    def _ensure_rc_token(self) -> str:
        """Get or refresh the RingCentral Platform API token."""
        if self._rc_token and time.time() < self._rc_token_expiry:
            return self._rc_token

        if not self.configured:
            raise RuntimeError("RingCX not configured — set RC_CLIENT_ID, RC_CLIENT_SECRET, RC_JWT_TOKEN")

        resp = requests.post(
            f"{self.server_url}/restapi/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self.jwt_token,
            },
            auth=(self.client_id, self.client_secret),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._rc_token = data["access_token"]
        self._rc_token_expiry = time.time() + data.get("expires_in", 3600) - 60
        log.info("RingEX auth token acquired (expires in %ds)", data.get("expires_in", 0))
        return self._rc_token

    def _rc_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_rc_token()}"}

    # ══════════════════════════════════════════════════════════════
    # Auth — RingCX (Engage Voice)
    # ══════════════════════════════════════════════════════════════

    def _ensure_cx_token(self) -> str:
        """Get or refresh the RingCX (Engage Voice) token.

        Two-step flow:
        1. Get RC platform token (reuse _ensure_rc_token)
        2. Exchange it for a RingCX token
        """
        if self._cx_token and time.time() < self._cx_token_expiry:
            return self._cx_token

        # Try refresh first if we have a refresh token
        if self._cx_refresh_token:
            try:
                return self._refresh_cx_token()
            except Exception:
                log.warning("RingCX refresh failed, doing full auth")

        # Full auth: get RC token then exchange
        rc_token = self._ensure_rc_token()

        resp = requests.post(
            f"{self.ringcx_url}/api/auth/login/rc/accesstoken?includeRefresh=true",
            data={
                "rcAccessToken": rc_token,
                "rcTokenType": "Bearer",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._cx_token = data.get("accessToken")
        self._cx_refresh_token = data.get("refreshToken")
        # RingCX tokens are valid for 5 minutes — refresh at 4 min to be safe
        self._cx_token_expiry = time.time() + 240
        log.info("RingCX token expires at +240s, refresh token: %s", "yes" if self._cx_refresh_token else "no")

        # Extract account ID from agentDetails
        agent_details = data.get("agentDetails") or []
        if agent_details and isinstance(agent_details, list):
            self._cx_account_id = str(agent_details[0].get("accountId", ""))
        if not self._cx_account_id:
            self._cx_account_id = str(data.get("accountId", ""))

        log.info("RingCX (Engage Voice) token acquired, account=%s", self._cx_account_id)
        return self._cx_token

    def _refresh_cx_token(self) -> str:
        """Refresh the RingCX token using the refresh token."""
        resp = requests.post(
            f"{self.ringcx_url}/api/auth/token/refresh",
            data={
                "refresh_token": self._cx_refresh_token,
                "rcTokenType": "Bearer",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._cx_token = data.get("accessToken")
        self._cx_refresh_token = data.get("refreshToken")  # must replace old one
        self._cx_token_expiry = time.time() + 240
        log.info("RingCX token refreshed")
        return self._cx_token

    def _cx_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_cx_token()}"}

    def cdr_diagnostics(self, start_dt: datetime, end_dt: datetime) -> dict:
        """Probe the reportsStreaming CDR endpoint with each available token and
        return the raw status + response body, so we can see exactly why it 403s
        (the body usually names the missing permission/scope). Diagnostic only."""
        out = {"account_id": None, "attempts": []}
        report_body = {
            "reportType": "GLOBAL_CALL_TYPE_DELIMITED",
            "reportCriteria": {
                "criteriaType": "GLOBAL_CALL_TYPE_CRITERIA",
                "startDate": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                "endDate": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                "containGates": True, "containCampaigns": True,
                "containIvrStudios": False, "containCloudProfiles": False,
                "containTracNumbers": False, "containAgents": True,
            },
        }
        try:
            cx_token = self._ensure_cx_token()
        except Exception as e:
            return {"error": f"CX token exchange failed: {e}"}
        out["account_id"] = self._cx_account_id
        url = f"{self.ringcx_url}/api/v1/admin/accounts/{self._cx_account_id}/reportsStreaming"
        tokens = [("jwt_exchanged_cx_token", cx_token)]
        if self.ringcx_api_token:
            tokens.append(("ringcx_api_token", self.ringcx_api_token))
        for label, tok in tokens:
            try:
                r = requests.post(url, headers={"Authorization": f"Bearer {tok}",
                                                "Content-Type": "application/json"},
                                  json=report_body, timeout=30)
                out["attempts"].append({
                    "token": label, "status": r.status_code,
                    "body": (r.text or "")[:600],
                })
            except Exception as e:
                out["attempts"].append({"token": label, "error": str(e)})
        return out

    # ══════════════════════════════════════════════════════════════
    # RingCX — Active Calls (Engage Voice API)
    # ══════════════════════════════════════════════════════════════

    def get_active_calls(self) -> list[dict]:
        """Fetch currently active calls from RingCX (Engage Voice).

        Returns a list of simplified call objects with agent info,
        caller details, call state, and duration.
        """
        try:
            token = self._ensure_cx_token()
            if not self._cx_account_id:
                log.warning("RingCX account ID not set, cannot fetch active calls")
                return []

            resp = requests.get(
                f"{self.ringcx_url}/voice/api/v1/admin/accounts/{self._cx_account_id}/activeCalls/list",
                headers=self._cx_headers(),
                params={
                    "product": "ACCOUNT",
                    "productId": self._cx_account_id,
                    "maxRows": 200,
                    "page": 1,
                },
                timeout=15,
            )
            if resp.status_code == 204:
                return []
            resp.raise_for_status()
            calls_data = resp.json()

            # Handle both list and dict responses
            if isinstance(calls_data, dict):
                calls_list = calls_data.get("activeCalls") or calls_data.get("records") or []
            elif isinstance(calls_data, list):
                calls_list = calls_data
            else:
                calls_list = []

            now = datetime.now(timezone.utc)
            active = []
            for call in calls_list:
                # Parse enqueue/start time for duration
                enqueue = call.get("enqueueTime") or call.get("callStartTime") or ""
                dur = 0
                if enqueue:
                    try:
                        started = datetime.fromisoformat(str(enqueue).replace("Z", "+00:00"))
                        dur = int((now - started).total_seconds())
                    except Exception:
                        pass

                agent_name = " ".join(filter(None, [
                    call.get("agentFirstName", ""),
                    call.get("agentLastName", ""),
                ])).strip()

                active.append({
                    "uii": call.get("uii", ""),
                    "session_id": call.get("uii", ""),
                    "call_state": call.get("callState", ""),
                    "status": call.get("callState", ""),
                    "direction": call.get("callDirection", call.get("direction", "")),
                    "ani": call.get("ani", ""),        # caller number
                    "dnis": call.get("dnis", ""),       # dialed number
                    "from": call.get("ani", ""),
                    "to": call.get("dnis", ""),
                    "agent_name": agent_name,
                    "agent_id": str(call.get("agentId", "")),
                    "queue_name": call.get("gate", call.get("queueName", "")),
                    "started_at": enqueue,
                    "duration_sec": max(0, dur),
                    "source": "ringcx",
                })

            log.info("RingCX: %d active calls found", len(active))
            return active

        except requests.exceptions.HTTPError as e:
            log.error("RingCX active calls error: %s — %s",
                      e, e.response.text[:300] if e.response is not None else "")
            return []
        except Exception as e:
            log.error("RingCX active calls error: %s", e)
            return []

    # ══════════════════════════════════════════════════════════════
    # RingCX — Call Monitoring (Engage Voice supervisor features)
    # ══════════════════════════════════════════════════════════════

    def monitor_call(self, uii: str, destination: str,
                     session_type: str = "MONITOR") -> dict:
        """Add a supervisor monitoring session to an active RingCX call.

        Uses query params (not JSON body) per RingCX API spec.
        Response is text/plain boolean.
        """
        type_map = {"BARGE_IN": "BARGEIN"}
        api_type = type_map.get(session_type, session_type)
        valid_types = ("MONITOR", "COACHING", "BARGEIN")
        if api_type not in valid_types:
            return {"error": f"Invalid session_type. Must be one of {valid_types}"}

        try:
            self._ensure_cx_token()
            if not self._cx_account_id:
                return {"error": "RingCX account ID not available"}

            import re
            dest = re.sub(r"\D", "", destination)
            if len(dest) == 10:
                dest = f"+1{dest}"
            elif len(dest) == 11 and dest[0] == "1":
                dest = f"+{dest}"

            resp = requests.post(
                f"{self.ringcx_url}/voice/api/v1/admin/accounts/{self._cx_account_id}"
                f"/activeCalls/{uii}/addSessionToCall",
                headers={
                    **self._cx_headers(),
                    "Accept": "text/plain",
                },
                params={
                    "destination": dest,
                    "sessionType": api_type,
                },
                timeout=15,
            )

            if resp.status_code in (200, 204):
                log.info("RingCX monitor: %s started on %s → %s",
                         api_type, uii, dest)
                return {
                    "status": "ok",
                    "session_type": api_type,
                    "uii": uii,
                    "destination": dest,
                }

            try:
                err_msg = resp.json().get("generalMessage") or resp.text[:200]
            except Exception:
                err_msg = resp.text[:200]

            log.error("RingCX monitor failed %d: %s", resp.status_code, err_msg)
            return {"error": f"RingCX returned {resp.status_code}: {err_msg}"}

        except Exception as e:
            log.error("RingCX monitor error: %s", e)
            return {"error": str(e)}

    # ══════════════════════════════════════════════════════════════
    # RingEX — Agent Presence (standard RC Platform API)
    # ══════════════════════════════════════════════════════════════

    def _fetch_extension_names(self) -> dict:
        """Fetch {ext_id: {name, extensionNumber}} for all enabled User extensions.
        Cached alongside the agents cache (same TTL).
        """
        if hasattr(self, "_ext_names") and self._ext_names and time.time() < self._agents_cache_expiry:
            return self._ext_names

        names = {}
        page = 1
        while True:
            resp = requests.get(
                f"{self.server_url}/restapi/v1.0/account/{self.account_id}/extension",
                headers=self._rc_headers(),
                params={"type": "User", "status": "Enabled", "perPage": 250, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            for ext in data.get("records", []):
                eid = str(ext.get("id", ""))
                names[eid] = {
                    "name": ext.get("name", ""),
                    "extensionNumber": ext.get("extensionNumber", ""),
                }
            nav = data.get("navigation", {})
            if nav.get("nextPage"):
                page += 1
            else:
                break
        log.info("RingEX: fetched names for %d extensions", len(names))
        self._ext_names = names
        return names

    def get_agent_statuses(self) -> list[dict]:
        """Fetch presence status for all extensions via RingEX API.

        Two-step approach:
        1. Fetch extension list (for names) — one paginated call
        2. Fetch account-level presence with detailedTelephonyState=true —
           one paginated call for all statuses + active call details

        This replaces the old per-extension presence loop that made N
        individual API calls and hit rate limits.
        Cached for 60 seconds.
        """
        if self._agents_cache and time.time() < self._agents_cache_expiry:
            return self._agents_cache

        try:
            # Step 1: get extension names
            ext_names = self._fetch_extension_names()

            # Step 2: get account-level presence with call details
            agents = []
            page = 1
            while True:
                resp = requests.get(
                    f"{self.server_url}/restapi/v1.0/account/{self.account_id}/presence",
                    headers=self._rc_headers(),
                    params={
                        "detailedTelephonyState": "true",
                        "perPage": 250,
                        "page": page,
                    },
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
                records = data.get("records", [])

                for p in records:
                    ext = p.get("extension") or {}
                    ext_id = str(ext.get("id") or p.get("id", ""))

                    # Look up name from extension list
                    ext_info = ext_names.get(ext_id, {})
                    name = ext_info.get("name") or ext.get("name") or p.get("name") or ""
                    ext_number = ext_info.get("extensionNumber") or ext.get("extensionNumber", "")

                    # Extract active call details with phone numbers
                    raw_calls = p.get("activeCalls") or []
                    active_calls = []
                    for ac in raw_calls:
                        active_calls.append({
                            "id": ac.get("id") or ac.get("telephonySessionId", ""),
                            "direction": ac.get("direction", ""),
                            "from": ac.get("from", ""),
                            "to": ac.get("to", ""),
                            "telephonyStatus": ac.get("telephonyStatus", ""),
                            "sipData": ac.get("sipData", {}),
                        })

                    agents.append({
                        "ext_id": ext_id,
                        "ext_number": ext_number,
                        "name": name,
                        "status": p.get("presenceStatus", "Offline"),
                        "dnd_status": p.get("dndStatus", ""),
                        "telephony_status": p.get("telephonyStatus", "NoCall"),
                        "active_calls": active_calls,
                    })

                nav = data.get("navigation") or data.get("paging") or {}
                total_pages = nav.get("totalPages", 1)
                if page < total_pages:
                    page += 1
                else:
                    break

            # Filter to only named agents (skip system/IVR extensions without names)
            agents = [a for a in agents if a["name"]]

            on_call = [a for a in agents if a["telephony_status"] in ("CallConnected", "Ringing", "OnHold")]
            log.info("RingEX: %d agents fetched, %d on call", len(agents), len(on_call))
            for a in on_call:
                log.info("  On call: %s (%s) — calls: %s", a["name"], a["telephony_status"],
                         a["active_calls"][:2] if a["active_calls"] else "[]")
            self._agents_cache = agents
            self._agents_cache_expiry = time.time() + self._AGENTS_CACHE_TTL
            return agents

        except Exception as e:
            log.error("RingEX agent statuses error: %s", e)
            return self._agents_cache if self._agents_cache else []

    # ══════════════════════════════════════════════════════════════
    # RingEX — Call History Search (account-level call log)
    # ══════════════════════════════════════════════════════════════

    def search_call_history(self, phone: str, days: int = 30,
                            direction: str = None) -> list[dict]:
        """Search the RingEX account-level call log for a phone number.

        This covers BOTH RingEX and RingCX calls since all telephony
        routes through the RC platform.

        Args:
            phone: Phone number to search (digits only, last 10 used).
                   Searches both from AND to fields.
            days: How many days back to search (max 180).
            direction: Optional filter — "Inbound", "Outbound", or None for both.

        Returns a list of simplified call records sorted by startTime desc.
        """
        try:
            token = self._ensure_rc_token()
            import re
            digits = re.sub(r"\D", "", phone)
            if len(digits) > 10:
                digits = digits[-10:]
            if len(digits) < 7:
                log.warning("search_call_history: phone too short: %s", phone)
                return []

            date_from = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            all_calls = []
            page = 1
            while page <= 10:
                params = {
                    "phoneNumber": digits,
                    "dateFrom": date_from,
                    "view": "Detailed",
                    "perPage": 250,
                    "page": page,
                }
                if direction:
                    params["direction"] = direction

                url = f"{self.server_url}/restapi/v1.0/account/{self.account_id}/call-log"
                log.info("RingEX call-log request: %s params=%s", url, params)
                resp = requests.get(
                    url,
                    headers=self._rc_headers(),
                    params=params,
                    timeout=20,
                )
                log.info("RingEX call-log response: %d, body=%s", resp.status_code, resp.text[:500])
                if resp.status_code == 204:
                    break
                resp.raise_for_status()
                data = resp.json()
                records = data.get("records", [])

                for rec in records:
                    from_obj = rec.get("from") or {}
                    to_obj = rec.get("to") or {}
                    recording = rec.get("recording") or {}

                    all_calls.append({
                        "id": rec.get("id"),
                        "session_id": rec.get("sessionId"),
                        "start_time": rec.get("startTime"),
                        "duration": rec.get("duration", 0),
                        "direction": rec.get("direction", ""),
                        "type": rec.get("type", ""),
                        "action": rec.get("action", ""),
                        "result": rec.get("result", ""),
                        "from_number": from_obj.get("phoneNumber", ""),
                        "from_name": from_obj.get("name", ""),
                        "to_number": to_obj.get("phoneNumber", ""),
                        "to_name": to_obj.get("name", ""),
                        "recording_id": recording.get("id"),
                        "recording_url": recording.get("contentUri"),
                        "source": "ringex",
                    })

                # The call-log response has no totalPages; a full page (== perPage) means
                # there may be more, a short page means we're done. Reading totalPages (default
                # 1) stopped every fetch at page 1 — the agent-seat calls sit on pages 2+.
                if len(records) < 250:
                    break
                page += 1

            log.info("RingEX call history: %d records for %s (last %d days)",
                     len(all_calls), digits, days)
            return all_calls

        except Exception as e:
            log.error("RingEX call history search error: %s", e)
            return []

    def search_ringcx_call_history(self, phone: str, days: int = 30):
        """Search RingCX (Engage Voice) for call detail records matching a phone.

        Uses the reportsStreaming endpoint with GLOBAL_CALL_TYPE_DELIMITED
        report to get historical RingCX-specific call data (queue names,
        agent details, talk time breakdowns).

        Falls back gracefully if the endpoint is unavailable.
        """
        try:
            token = self._ensure_cx_token()
            if not self._cx_account_id:
                return []

            import re
            digits = re.sub(r"\D", "", phone)
            if len(digits) > 10:
                digits = digits[-10:]

            date_to = datetime.now(timezone.utc)
            date_from = date_to - __import__("datetime").timedelta(days=days)

            report_url = f"{self.ringcx_url}/api/v1/admin/accounts/{self._cx_account_id}/reportsStreaming"
            report_body = {
                "reportType": "GLOBAL_CALL_TYPE_DELIMITED",
                "reportCriteria": {
                    "criteriaType": "GLOBAL_CALL_TYPE_CRITERIA",
                    "startDate": date_from.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                    "endDate": date_to.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                    "containGates": True,
                    "containCampaigns": True,
                    "containIvrStudios": False,
                    "containCloudProfiles": False,
                    "containTracNumbers": False,
                    "containAgents": True,
                },
            }
            resp = requests.post(
                report_url,
                headers={**self._cx_headers(), "Content-Type": "application/json"},
                json=report_body,
                timeout=30,
            )
            if resp.status_code == 403 and self.ringcx_api_token:
                log.info("RingCX CDR 403 with JWT token, retrying with API token")
                resp = requests.post(
                    report_url,
                    headers={
                        "Authorization": f"Bearer {self.ringcx_api_token}",
                        "Content-Type": "application/json",
                    },
                    json=report_body,
                    timeout=30,
                )
            if resp.status_code in (204, 404, 400):
                log.info("RingCX CDR: no data or endpoint unavailable (%d)", resp.status_code)
                return []
            resp.raise_for_status()

            # Response is delimited text (CSV-like)
            text = resp.text
            if not text or not text.strip():
                return []

            lines = text.strip().split("\n")
            if len(lines) < 2:
                return []

            # Parse header
            header = lines[0].split("|")
            header = [h.strip().strip('"') for h in header]

            cx_calls = []
            for line in lines[1:]:
                fields = line.split("|")
                fields = [f.strip().strip('"') for f in fields]
                row = dict(zip(header, fields))

                ani = row.get("ANI", "").replace("+", "").replace("-", "")
                dnis = row.get("DNIS", "").replace("+", "").replace("-", "")

                # Filter: only keep records matching our phone
                ani_match = ani[-10:] == digits if len(ani) >= 10 else False
                dnis_match = dnis[-10:] == digits if len(dnis) >= 10 else False
                if not ani_match and not dnis_match:
                    continue

                duration = 0
                try:
                    duration = int(float(row.get("Call Duration", "0") or "0"))
                except (ValueError, TypeError):
                    pass

                cx_calls.append({
                    "id": row.get("UII", ""),
                    "session_id": row.get("UII", ""),
                    "start_time": row.get("Enqueue Time", row.get("Call Start", "")),
                    "duration": duration,
                    "direction": row.get("Call Direction", ""),
                    "type": "Voice",
                    "action": row.get("Call Type", ""),
                    "result": row.get("Call Result", row.get("Call Status", "")),
                    "from_number": ani,
                    "from_name": "",
                    "to_number": dnis,
                    "to_name": "",
                    "agent_name": row.get("Agent Name", ""),
                    "queue_name": row.get("Queue Name", row.get("Gate Name", "")),
                    "talk_time": row.get("Talk Time", ""),
                    "hold_time": row.get("Hold Time", ""),
                    "recording_url": row.get("Recording URL", None),
                    "source": "ringcx",
                })

            log.info("RingCX CDR: %d records for %s (last %d days)",
                     len(cx_calls), digits, days)
            return cx_calls

        except Exception as e:
            log.error("RingCX CDR search error: %s", e)
            return []

    # ══════════════════════════════════════════════════════════════
    # Combined live snapshot (both APIs)
    # ══════════════════════════════════════════════════════════════

    def get_live_snapshot(self) -> dict:
        """Return a combined live monitoring snapshot from both RingEX and RingCX."""
        # RingCX active calls (Engage Voice — detailed call data)
        cx_calls = self.get_active_calls()

        # RingEX agent presence (standard RC — who's available/busy/on call)
        agents = self.get_agent_statuses()

        # Agents currently on a call (from RingEX presence)
        on_call = [a for a in agents if a["telephony_status"] in ("CallConnected", "Ringing", "OnHold")]
        available = [a for a in agents if a["status"] == "Available" and a["telephony_status"] == "NoCall"]
        dnd = [a for a in agents if a["dnd_status"] == "DoNotAcceptAnyCalls" or a["status"] == "DoNotDisturb"]
        offline = [a for a in agents if a["status"] == "Offline"]

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "active_calls": cx_calls,          # from RingCX (Engage Voice)
            "agents": agents,                   # from RingEX (presence)
            "summary": {
                "total_agents": len(agents),
                "on_call": len(on_call),
                "available": len(available),
                "dnd": len(dnd),
                "offline": len(offline),
                "active_call_count": len(cx_calls),
            },
        }

    # ══════════════════════════════════════════════════════════════
    # RingEX — bulk outbound call log for fallback matching
    # ══════════════════════════════════════════════════════════════

    def fetch_todays_outbound_calls(self) -> dict[str, list[dict]]:
        """Fetch today's outbound calls from RingEX call log, keyed by last-10-digit phone.

        Returns {normalized_phone: [call_records]} for all outbound calls
        placed today.  Used as a fallback when Zoho has no call record for
        a scheduled call that RingCX actually dialed.
        """
        import re
        try:
            self._ensure_rc_token()
            tz_offset = int(os.environ.get("TZ_OFFSET_HOURS", "-4"))
            now_utc = datetime.now(timezone.utc)
            local_now = now_utc + timedelta(hours=tz_offset)
            local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            date_from = (local_start - timedelta(hours=tz_offset)).isoformat()

            phone_map: dict[str, list[dict]] = {}
            page = 1
            max_pages = 10  # 2500 records — the Heavy call-log rate cap is ~10 req/min

            while page <= max_pages:
                resp = requests.get(
                    f"{self.server_url}/restapi/v1.0/account/{self.account_id}/call-log",
                    headers=self._rc_headers(),
                    params={
                        "direction": "Outbound",
                        "dateFrom": date_from,
                        "view": "Simple",
                        "perPage": 250,
                        "page": page,
                    },
                    timeout=20,
                )
                if resp.status_code == 204:
                    break
                if not resp.ok:
                    log.warning("RingEX bulk call-log page %d failed: %s", page, resp.status_code)
                    break
                data = resp.json()
                records = data.get("records", [])

                for rec in records:
                    to_obj = rec.get("to") or {}
                    to_num = to_obj.get("phoneNumber") or ""
                    digits = re.sub(r"\D", "", to_num)
                    norm = digits[-10:] if len(digits) >= 10 else ""
                    if not norm:
                        continue
                    from_obj = rec.get("from") or {}
                    agent_name = from_obj.get("name") or ""
                    phone_map.setdefault(norm, []).append({
                        "id": rec.get("id"),
                        "start_time": rec.get("startTime"),
                        "duration": rec.get("duration", 0),
                        "result": rec.get("result", ""),
                        "direction": "Outbound",
                        "to_number": to_num,
                        "agent": agent_name,
                        "source": "ringex",
                    })

                # The call-log response has no totalPages; a full page (== perPage) means
                # there may be more, a short page means we're done. Reading totalPages (default
                # 1) stopped every fetch at page 1 — the agent-seat calls sit on pages 2+.
                if len(records) < 250:
                    break
                page += 1

            total = sum(len(v) for v in phone_map.values())
            log.info("RingEX bulk outbound: %d calls across %d phones (%d pages)",
                     total, len(phone_map), page)
            return phone_map

        except Exception as e:
            log.error("RingEX bulk outbound fetch error: %s", e)
            return {}

    # ══════════════════════════════════════════════════════════════
    # Per-agent call analytics (RingCX CDR + RingEX call-log)
    # ══════════════════════════════════════════════════════════════

    def _fetch_ringcx_cdr_rows(self, start_dt: datetime, end_dt: datetime) -> list[dict]:
        """All RingCX (Engage Voice) CDR rows in [start_dt, end_dt].

        Same GLOBAL_CALL_TYPE_DELIMITED report as search_ringcx_call_history,
        but WITHOUT the single-phone filter — returns every call with its
        Agent Name, so we can roll up per-agent stats.
        """
        try:
            self._ensure_cx_token()
            if not self._cx_account_id:
                return []
            report_url = f"{self.ringcx_url}/api/v1/admin/accounts/{self._cx_account_id}/reportsStreaming"
            report_body = {
                "reportType": "GLOBAL_CALL_TYPE_DELIMITED",
                "reportCriteria": {
                    "criteriaType": "GLOBAL_CALL_TYPE_CRITERIA",
                    "startDate": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                    "endDate": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                    "containGates": True,
                    "containCampaigns": True,
                    "containIvrStudios": False,
                    "containCloudProfiles": False,
                    "containTracNumbers": False,
                    "containAgents": True,
                },
            }
            resp = requests.post(
                report_url,
                headers={**self._cx_headers(), "Content-Type": "application/json"},
                json=report_body,
                timeout=45,
            )
            # If JWT-exchanged token gets 403, retry with RingCX API token
            if resp.status_code == 403 and self.ringcx_api_token:
                log.info("RingCX CDR 403 with JWT token, retrying with API token")
                resp = requests.post(
                    report_url,
                    headers={
                        "Authorization": f"Bearer {self.ringcx_api_token}",
                        "Content-Type": "application/json",
                    },
                    json=report_body,
                    timeout=45,
                )
            if resp.status_code in (204, 400, 404):
                log.info("RingCX CDR (all): no data / unavailable (%d)", resp.status_code)
                return []
            resp.raise_for_status()
            text = resp.text or ""
            lines = text.strip().split("\n")
            if len(lines) < 2:
                return []
            header = [h.strip().strip('"') for h in lines[0].split("|")]
            rows = []
            for line in lines[1:]:
                fields = [f.strip().strip('"') for f in line.split("|")]
                row = dict(zip(header, fields))
                try:
                    duration = int(float(row.get("Call Duration", "0") or "0"))
                except (ValueError, TypeError):
                    duration = 0
                rec = {
                    "agent_name": row.get("Agent Name", "") or "",
                    "duration": duration,
                    "direction": row.get("Call Direction", "") or "",
                    "result": row.get("Call Result", row.get("Call Status", "")) or "",
                    "start_time": row.get("Enqueue Time", row.get("Call Start", "")) or "",
                    "source": "ringcx",
                }
                # Carry extra detail when available for raw-interactions view
                for extra_key, col_names in [
                    ("uii", ["UII"]),
                    ("ani", ["ANI"]),
                    ("dnis", ["DNIS"]),
                    ("agent_disposition", ["Agent Disposition"]),
                    ("talk_time", ["Talk Time"]),
                    ("hold_time", ["Hold Time"]),
                    ("ring_time", ["Ring Time"]),
                    ("queue_name", ["Queue Name", "Gate Name"]),
                    ("campaign_name", ["Campaign Name", "Campaign"]),
                    ("call_type", ["Call Type"]),
                    ("agent_id", ["Agent ID", "Agent Id"]),
                    ("dequeue_time", ["Dequeue Time"]),
                ]:
                    for cn in col_names:
                        v = row.get(cn, "")
                        if v:
                            rec[extra_key] = v
                            break
                rows.append(rec)
            log.info("RingCX CDR (all): %d rows %s..%s",
                     len(rows), start_dt.date(), end_dt.date())
            return rows
        except Exception as e:
            log.error("RingCX CDR (all) error: %s", e)
            return []

    def _fetch_ringex_agent_calls(self, start_dt: datetime, end_dt: datetime) -> list[dict]:
        """All RingEX platform call-log calls in [start_dt, end_dt], attributed
        to the internal user extension on the call.

        Each call is credited to the agent whose extension placed (outbound) or
        answered (inbound) it, resolved against the User-extension roster. Calls
        with no internal extension (pure external/queue legs) get no agent and
        are dropped by the caller.
        """
        try:
            self._ensure_rc_token()
            ext_names = self._fetch_extension_names()  # {ext_id: {name, extensionNumber}}
            date_from = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            date_to = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            out: list[dict] = []
            page = 1
            # The account call-log is in RingCentral's "Heavy" class, ~10 requests
            # per minute. fetch_todays_outbound_calls already caps at 10 for exactly
            # this reason; 20 here meant a SINGLE report could blow the whole minute's
            # budget and come back 429 -> empty, which reads as a quiet phone.
            max_pages = 10  # 2500 records
            # Tells the caller the answer is partial. A truncated fetch that looks
            # complete is worse than one that admits it.
            self.last_ringex_note = None

            def _get(page_no):
                """One page, with a single Retry-After-honouring retry on 429."""
                params = {
                    "dateFrom": date_from, "dateTo": date_to,
                    # Simple view — carries name/duration/result/direction (all the
                    # report needs) and is lighter than Detailed for the account-wide,
                    # no-phone query.
                    "view": "Simple", "perPage": 250, "page": page_no,
                }
                url = f"{self.server_url}/restapi/v1.0/account/{self.account_id}/call-log"
                r = requests.get(url, headers=self._rc_headers(), params=params, timeout=25)
                if r.status_code == 429:
                    wait = 5.0
                    try:
                        wait = float(r.headers.get("Retry-After") or 5)
                    except (TypeError, ValueError):
                        pass
                    wait = min(max(wait, 1.0), 30.0)
                    log.warning("RingEX call-log 429 on page %d; waiting %.0fs", page_no, wait)
                    time.sleep(wait)
                    r = requests.get(url, headers=self._rc_headers(), params=params, timeout=25)
                return r

            while page <= max_pages:
                resp = _get(page)
                if resp.status_code == 204:
                    break
                if not resp.ok:
                    self.last_ringex_note = (
                        "RingEX call-log returned HTTP %d on page %d; the figures below "
                        "cover only what was fetched before that."
                        % (resp.status_code, page)
                        if resp.status_code != 429 else
                        "RingEX is rate limiting us (HTTP 429). The account call-log allows "
                        "about 10 requests a minute and this window needed more, so RingEX "
                        "calls are missing or incomplete.")
                    log.warning("RingEX call-log page %d failed: %s", page, resp.status_code)
                    break
                data = resp.json()
                records = data.get("records", [])
                for rec in records:
                    frm = rec.get("from") or {}
                    to = rec.get("to") or {}
                    direction = rec.get("direction", "") or ""
                    from_ext = str(frm.get("extensionId") or "")
                    to_ext = str(to.get("extensionId") or "")
                    # The agent is OUR side of the leg. Outbound: we→patient, so the agent
                    # is `from`; Inbound: patient→us, so the agent is `to`. Read the leg's
                    # own display NAME — RingCentral fills the user's name there, and it is
                    # the source that actually works (this is exactly how the board's bulk
                    # fetch attributes calls). The previous version resolved extensionId
                    # against the roster and, when those IDs weren't populated / didn't
                    # match, left EVERY call unattributed — which is why the report read 0.
                    if direction == "Outbound":
                        agent = (frm.get("name") or "").strip()
                    elif direction == "Inbound":
                        agent = (to.get("name") or "").strip()
                    else:
                        agent = ""
                    # Fallbacks for odd/queue legs: the extension roster, then an inner leg.
                    if not agent:
                        if from_ext in ext_names:
                            agent = ext_names[from_ext]["name"]
                        elif to_ext in ext_names:
                            agent = ext_names[to_ext]["name"]
                        else:
                            for leg in (rec.get("legs") or []):
                                lf = str((leg.get("from") or {}).get("extensionId") or "")
                                lt = str((leg.get("to") or {}).get("extensionId") or "")
                                if lf and lf in ext_names:
                                    agent = ext_names[lf]["name"]; break
                                if lt and lt in ext_names:
                                    agent = ext_names[lt]["name"]; break
                    from_num = frm.get("phoneNumber", "") or ""
                    to_num = to.get("phoneNumber", "") or ""
                    out.append({
                        "agent_name": agent or "",
                        "duration": rec.get("duration", 0) or 0,
                        "direction": direction,
                        "result": rec.get("result", "") or "",
                        "start_time": rec.get("startTime", "") or "",
                        "from_number": from_num,
                        "to_number": to_num,
                        "action": rec.get("action", "") or "",
                        "type": rec.get("type", "") or "",
                        "session_id": rec.get("sessionId", "") or "",
                        "source": "ringex",
                    })
                # The call-log response has no totalPages; a full page (== perPage) means
                # there may be more, a short page means we're done. Reading totalPages (default
                # 1) stopped every fetch at page 1 — the agent-seat calls sit on pages 2+.
                if len(records) < 250:
                    break
                page += 1
                time.sleep(1.2)   # pace inside the ~10/min Heavy budget
            else:
                self.last_ringex_note = (
                    "RingEX call-log hit the %d-page cap; there are more calls in this "
                    "window than were fetched." % max_pages)
                log.warning("RingEX call-log truncated at %d pages", max_pages)
            log.info("RingEX call-log (all): %d calls %s..%s",
                     len(out), start_dt.date(), end_dt.date())
            return out
        except Exception as e:
            # Must leave a note. Returning [] silently is how a broken fetch and a
            # quiet phone became indistinguishable for 41 days; a caller persisting
            # this result would bake that ambiguity onto disk.
            self.last_ringex_note = (
                "RingEX call-log request failed: %s. No calls could be read, which is "
                "not the same as there being none." % e)
            log.error("RingEX call-log (all) error: %s", e)
            return []

    @staticmethod
    def _to_ts(value: str) -> Optional[float]:
        if not value:
            return None
        try:
            v = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _metric_value(metric) -> int:
        """Read a scalar out of a RingCentral analytics counter/timer value,
        tolerating the few shapes the API uses: {"value": N},
        {"values": [{"value": N}, ...]}, or a bare number.
        """
        if metric is None:
            return 0
        if isinstance(metric, (int, float)):
            return int(metric)
        if isinstance(metric, dict):
            if isinstance(metric.get("value"), (int, float)):
                return int(metric["value"])
            vals = metric.get("values")
            if isinstance(vals, list):
                total = 0
                for v in vals:
                    if isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
                        total += v["value"]
                    elif isinstance(v, (int, float)):
                        total += v
                return int(total)
        return 0

    def _fetch_analytics_aggregate(self, start_dt: datetime, end_dt: datetime) -> dict:
        """Per-agent calls + talk time from the RingCentral Business Analytics
        API (Call Performance), grouped by Users.

        POST /analytics/phone/performance/v1/accounts/{id}/calls/aggregate
        This is account-wide and covers ALL telephony (RingEX + RingCX), so it
        is the authoritative per-agent interaction source. Returns
        {agent_name: {"calls": int, "talk_seconds": int}}; empty dict if the
        analytics API is unavailable (caller then falls back to CDR/call-log).
        """
        try:
            self._ensure_rc_token()
            ext_names = self._fetch_extension_names()  # {ext_id: {name, ...}}

            def _body(with_handle: bool) -> dict:
                timers = {"allCallsDuration": {"aggregationType": "Sum"}}
                if with_handle:
                    # Total handle time (talk + hold + wrap). Not every account
                    # exposes this timer on the aggregate endpoint, so the caller
                    # self-heals to talk-only if the request is rejected.
                    timers["handlingDuration"] = {"aggregationType": "Sum"}
                return {
                    "grouping": {"groupBy": "Users"},
                    "timeRange": {
                        "timeFrom": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "timeTo": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    },
                    "responseOptions": {
                        "counters": {"allCalls": {"aggregationType": "Sum"}},
                        "timers": timers,
                    },
                }

            want_handle = True
            out: dict[str, dict] = {}
            page = 1
            while page <= 20:
                resp = requests.post(
                    f"{self.server_url}/analytics/phone/performance/v1/accounts/{self.account_id}/calls/aggregate",
                    headers={**self._rc_headers(), "Content-Type": "application/json"},
                    params={"perPage": 1000, "page": page},
                    json=_body(want_handle),
                    timeout=30,
                )
                if resp.status_code == 204:
                    break
                if not resp.ok:
                    # If the handle-time timer is what the API rejected, drop it
                    # and retry the SAME page with talk-time only.
                    if want_handle:
                        log.info("Analytics handle-time timer rejected (%s) — retrying talk-only",
                                 resp.status_code)
                        want_handle = False
                        continue
                    log.warning("Analytics aggregate failed %s: %s",
                                resp.status_code, resp.text[:200])
                    return {}
                data = resp.json() or {}
                records = (data.get("data") or {}).get("records") or data.get("records") or []
                for rec in records:
                    key = rec.get("key")
                    if isinstance(key, dict):
                        uid = str(key.get("id") or key.get("key") or "")
                        kname = key.get("name") or ""
                    else:
                        uid = str(key or "")
                        kname = ""
                    name = (ext_names.get(uid, {}).get("name") or kname or "").strip()
                    if not name:
                        continue
                    tmr = rec.get("timers") or {}
                    calls = self._metric_value((rec.get("counters") or {}).get("allCalls"))
                    talk = self._metric_value(tmr.get("allCallsDuration"))
                    handle = self._metric_value(tmr.get("handlingDuration")) if want_handle else 0
                    a = out.setdefault(name, {"calls": 0, "talk_seconds": 0, "handle_seconds": 0})
                    a["calls"] += calls
                    a["talk_seconds"] += talk
                    a["handle_seconds"] += handle
                paging = data.get("paging") or {}
                if page < (paging.get("totalPages") or 1):
                    page += 1
                else:
                    break
            log.info("Analytics aggregate: %d agents %s..%s",
                     len(out), start_dt.date(), end_dt.date())
            return out
        except Exception as e:
            log.error("Analytics aggregate error: %s", e)
            return {}

    def get_agent_call_stats(
        self,
        start_dt: datetime,
        end_dt: datetime,
        long_call_seconds: int = 180,
    ) -> dict:
        """Per-agent call stats for all sales agents.

        Source of truth = real per-call detail: the RingCX (Engage Voice) CDR
        (agent-attributed; available once WEM/Data-Management access is enabled)
        PLUS the RingEX platform call log, which also carries the dialer legs
        that ride the RingCentral platform. The Business Analytics aggregate is
        only a last resort — it materially undercounts dialer activity.

        Returns {"agents": {name: {calls, calls_under_3m, calls_over_3m,
        talk_seconds, handle_seconds}}, "source": str, "unattributed": int}.
        Calls that can't be tied to a known agent are excluded but counted in
        "unattributed" so callers can surface the gap.
        """
        by_agent: dict[str, dict] = {}
        _canon: dict[str, str] = {}  # lowercase → canonical display name
        seen: list[tuple[str, float]] = []  # (agent_lower, start_ts)
        unattributed = 0

        def _resolve(name: str) -> str:
            low = name.lower()
            if low not in _canon:
                _canon[low] = name
            return _canon[low]

        def add(agent: str, duration, ts: Optional[float], handle=None):
            key = _resolve(agent)
            a = by_agent.setdefault(key, {
                "calls": 0, "calls_under_3m": 0, "calls_over_3m": 0,
                "calls_under_15m": 0,
                "talk_seconds": 0, "handle_seconds": 0,
            })
            try:
                dur = int(duration or 0)
            except (TypeError, ValueError):
                dur = 0
            if dur < 0:
                dur = 0
            a["calls"] += 1
            a["talk_seconds"] += dur
            # Handle time only comes from the CDR (talk+hold). The call log has
            # no separate handle metric, so leave it 0 there rather than faking
            # it as the call duration.
            a["handle_seconds"] += int(handle) if handle else 0
            if dur >= long_call_seconds:
                a["calls_over_3m"] += 1
            else:
                a["calls_under_3m"] += 1
            # Cumulative bucket: calls under 15 min (900s). calls_under_3m already
            # covers <180s because the caller passes long_call_seconds=180.
            if dur < 900:
                a["calls_under_15m"] += 1
            if ts is not None:
                seen.append((key.lower(), ts))

        # RingCX CDR — agent-attributed dials with a real talk/hold breakdown
        # (empty when WEM/Data-Management access is blocked → 403).
        for c in self._fetch_ringcx_cdr_rows(start_dt, end_dt):
            agent = (c.get("agent_name") or "").strip()
            if not agent:
                unattributed += 1
                continue
            handle = 0
            for k in ("talk_time", "hold_time"):
                try:
                    handle += int(float(c.get(k) or 0))
                except (TypeError, ValueError):
                    pass
            add(agent, c.get("duration"), self._to_ts(c.get("start_time")),
                handle=handle or None)

        # RingEX platform call log — carries the dialer legs; supplements with
        # anything the CDR didn't already count.
        for c in self._fetch_ringex_agent_calls(start_dt, end_dt):
            agent = (c.get("agent_name") or "").strip()
            ts = self._to_ts(c.get("start_time"))
            if not agent:
                unattributed += 1
                continue
            if ts is not None and any(
                al == agent.lower() and abs(ts - st) < 120 for al, st in seen
            ):
                continue  # same dial already counted from RingCX
            add(agent, c.get("duration"), ts)

        if by_agent:
            log.info("Agent call stats: %d agents, %d unattributed (source=cdr+calllog)",
                     len(by_agent), unattributed)
            return {"agents": by_agent, "source": "cdr+calllog",
                    "unattributed": unattributed}

        # Last resort only: Business Analytics aggregate (undercounts dialer).
        log.info("No call-detail rows — falling back to analytics aggregate")
        agg = self._fetch_analytics_aggregate(start_dt, end_dt)
        for name, a in agg.items():
            a.setdefault("calls_under_3m", 0)
            a.setdefault("calls_over_3m", 0)
            a.setdefault("calls_under_15m", 0)
            a.setdefault("handle_seconds", 0)
        return {"agents": agg, "source": "analytics" if agg else "none",
                "unattributed": unattributed}

    # ══════════════════════════════════════════════════════════════
    # Per-EXTENSION call log — the billing board's only source
    # ══════════════════════════════════════════════════════════════

    def fetch_extension_calls(self, ext_id, start_dt: datetime, end_dt: datetime,
                              max_pages: int = 12, max_wait: float = 30.0,
                              timeout: float = 30.0) -> tuple[list[dict], dict]:
        """Every call leg on ONE extension in [start_dt, end_dt].

        Why per-extension and not the account-wide log: the account endpoint is
        RingCentral's "Heavy" class, caps out around 2,500 records, and truncates
        without saying so -- a scan of it is not evidence of absence. This one is
        cheap (~0.4s/page), reaches at least 180 days back, and every row arrives
        pre-attributed to the seat that made it, so there is no name matching.

        Returns (rows, meta). meta carries `truncated` and `note`; a partial fetch
        that looks complete is worse than one that admits it, and this feeds a
        board that names individuals.
        """
        rows: dict[str, dict] = {}
        meta = {"pages": 0, "truncated": False, "note": None, "http_error": None}
        try:
            self._ensure_rc_token()
            page = 1
            while page <= max_pages:
                params = {
                    "dateFrom": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "dateTo": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "view": "Simple", "perPage": 250, "page": page,
                }
                url = (f"{self.server_url}/restapi/v1.0/account/{self.account_id}"
                       f"/extension/{ext_id}/call-log")
                r = requests.get(url, headers=self._rc_headers(), params=params, timeout=timeout)
                if r.status_code == 429:
                    # max_wait lets a caller that is serving a web request refuse
                    # to sit on a 60s Retry-After. Waiting it out here once cost
                    # 81 seconds of a request that gunicorn kills at 90.
                    if max_wait <= 0:
                        meta["http_error"] = 429
                        meta["note"] = ("RingEX is rate limiting us (HTTP 429) and this fetch "
                                        "will not wait it out; the day was left unfetched.")
                        break
                    wait = 5.0
                    try:
                        wait = float(r.headers.get("Retry-After") or 5)
                    except (TypeError, ValueError):
                        pass
                    wait = min(max(wait, 1.0), max_wait)
                    log.warning("ext %s call-log 429 on page %d; waiting %.0fs",
                                ext_id, page, wait)
                    time.sleep(wait)
                    r = requests.get(url, headers=self._rc_headers(), params=params,
                                     timeout=timeout)
                if r.status_code == 204:
                    break
                if not r.ok:
                    meta["http_error"] = r.status_code
                    meta["note"] = (
                        "RingEX is rate limiting us (HTTP 429); this seat's figures are "
                        "incomplete." if r.status_code == 429 else
                        f"RingEX call-log returned HTTP {r.status_code} for extension "
                        f"{ext_id}; this seat's figures cover only what was fetched first.")
                    log.warning("ext %s call-log page %d failed: %s", ext_id, page, r.status_code)
                    break
                recs = r.json().get("records", [])
                meta["pages"] = page
                for rec in recs:
                    frm, to = rec.get("from") or {}, rec.get("to") or {}
                    rows[rec.get("id") or f"{page}:{len(rows)}"] = {
                        "id": rec.get("id", ""),
                        "session_id": rec.get("sessionId", ""),
                        "duration": rec.get("duration", 0) or 0,
                        "direction": rec.get("direction", "") or "",
                        "result": rec.get("result", "") or "",
                        "start_time": rec.get("startTime", "") or "",
                        "from_number": frm.get("phoneNumber", "") or "",
                        "to_number": to.get("phoneNumber", "") or "",
                        "from_name": frm.get("name", "") or "",
                        "to_name": to.get("name", "") or "",
                        "action": rec.get("action", "") or "",
                        "source": "ringex",
                    }
                if len(recs) < 250:
                    break
                page += 1
                time.sleep(0.35)
            else:
                meta["truncated"] = True
                meta["note"] = (f"Hit the {max_pages}-page cap for extension {ext_id}; there are "
                                f"more calls in this window than were fetched.")
                log.warning("ext %s call-log truncated at %d pages", ext_id, max_pages)
        except Exception as e:  # noqa: BLE001
            # Never return [] silently. A failed fetch and a quiet phone are the
            # same shape, and this one feeds a per-person scoreboard.
            meta["note"] = (f"RingEX call-log request failed for extension {ext_id}: {e}. "
                            f"No calls could be read, which is not the same as there being none.")
            log.error("ext %s call-log error: %s", ext_id, e)
        return list(rows.values()), meta

    # ══════════════════════════════════════════════════════════════
    # SMS — send via RingCentral Platform API
    # ══════════════════════════════════════════════════════════════

    def send_sms(self, to_number: str, text: str, from_number: str = "") -> dict:
        """Send an SMS via the RingCentral platform API.

        Returns {"id": message_id, ...} on success or raises RuntimeError.
        """
        log = logging.getLogger(__name__)
        if not from_number:
            from_number = os.getenv("RC_SMS_FROM_NUMBER", "")
        if not from_number:
            raise RuntimeError("RC_SMS_FROM_NUMBER not configured")
        if not to_number:
            raise RuntimeError("No recipient phone number")

        to_e164 = to_number if to_number.startswith("+") else f"+1{to_number.lstrip('+')}"
        from_e164 = from_number if from_number.startswith("+") else f"+1{from_number.lstrip('+')}"

        resp = requests.post(
            f"{self.server_url}/restapi/v1.0/account/{self.account_id}/extension/~/sms",
            headers={**self._rc_headers(), "Content-Type": "application/json"},
            json={
                "from": {"phoneNumber": from_e164},
                "to": [{"phoneNumber": to_e164}],
                "text": text,
            },
            timeout=15,
        )
        if not resp.ok:
            log.error("RC SMS send failed %s: %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"SMS send failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        log.info("SMS sent to %s (id=%s)", to_e164, data.get("id"))
        return data
