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
from datetime import datetime, timezone
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

        Args:
            uii: Unique Interaction Identifier of the active call.
            destination: Phone number to ring the supervisor on (E.164 or 10-digit).
            session_type: One of "MONITOR" (silent listen), "COACHING" (whisper
                          to agent only), "BARGE_IN" (full 3-way).

        Returns dict with status/error info.
        """
        valid_types = ("MONITOR", "COACHING", "BARGE_IN")
        if session_type not in valid_types:
            return {"error": f"Invalid session_type. Must be one of {valid_types}"}

        try:
            token = self._ensure_cx_token()
            if not self._cx_account_id:
                return {"error": "RingCX account ID not available"}

            import re
            dest_digits = re.sub(r"\D", "", destination)
            if len(dest_digits) == 10:
                dest_digits = "1" + dest_digits  # prepend US country code

            resp = requests.post(
                f"{self.ringcx_url}/api/v1/admin/accounts/{self._cx_account_id}"
                f"/activeCalls/{uii}/addSessionToCall",
                headers={
                    **self._cx_headers(),
                    "Content-Type": "application/json",
                },
                params={
                    "destination": dest_digits,
                    "sessionType": session_type,
                },
                timeout=15,
            )

            if resp.status_code == 200 or resp.status_code == 204:
                log.info("RingCX monitor: %s session started on call %s → %s",
                         session_type, uii, dest_digits)
                return {
                    "status": "ok",
                    "session_type": session_type,
                    "uii": uii,
                    "destination": dest_digits,
                }

            # Try to parse error
            try:
                err_data = resp.json()
                err_msg = err_data.get("message") or err_data.get("error") or resp.text[:200]
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
            # Normalize to E.164-ish (no leading +, just digits)
            import re
            digits = re.sub(r"\D", "", phone)
            if len(digits) > 10:
                digits = digits[-10:]  # strip country code
            if len(digits) < 7:
                log.warning("search_call_history: phone too short: %s", phone)
                return []

            date_from = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).isoformat()

            all_calls = []
            page = 1
            while page <= 10:  # safety cap
                params = {
                    "phoneNumber": digits,
                    "dateFrom": date_from,
                    "view": "Simple",
                    "perPage": 250,
                    "page": page,
                }
                if direction:
                    params["direction"] = direction

                resp = requests.get(
                    f"{self.server_url}/restapi/v1.0/account/{self.account_id}/call-log",
                    headers=self._rc_headers(),
                    params=params,
                    timeout=20,
                )
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

                nav = data.get("navigation", {})
                if page < nav.get("totalPages", 1):
                    page += 1
                else:
                    break

            log.info("RingEX call history: %d records for %s (last %d days)",
                     len(all_calls), digits, days)
            return all_calls

        except Exception as e:
            log.error("RingEX call history search error: %s", e)
            return []

    def search_ringcx_call_history(self, phone: str, days: int = 30) -> list[dict]:
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

            resp = requests.post(
                f"{self.ringcx_url}/api/v1/admin/accounts/{self._cx_account_id}/reportsStreaming",
                headers={
                    **self._cx_headers(),
                    "Content-Type": "application/json",
                },
                json={
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
                },
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
