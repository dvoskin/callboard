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
        # RingCX tokens are valid for 5 minutes
        self._cx_token_expiry = time.time() + 240  # refresh at 4 min to be safe

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
    # RingEX — Agent Presence (standard RC Platform API)
    # ══════════════════════════════════════════════════════════════

    def get_agent_statuses(self) -> list[dict]:
        """Fetch presence status for all extensions via RingEX API.

        Returns:
        [
            {
                "ext_id": "12345",
                "name": "Jane Doe",
                "status": "Available" | "Busy" | "DoNotDisturb" | "Offline",
                "telephony_status": "NoCall" | "Ringing" | "OnHold" | "CallConnected",
                "active_calls": [...],
            },
        ]
        """
        try:
            all_extensions = []
            page = 1
            while True:
                resp = requests.get(
                    f"{self.server_url}/restapi/v1.0/account/{self.account_id}/extension",
                    headers=self._rc_headers(),
                    params={
                        "type": "User",
                        "status": "Enabled",
                        "perPage": 100,
                        "page": page,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                all_extensions.extend(data.get("records", []))
                nav = data.get("navigation", {})
                if nav.get("nextPage"):
                    page += 1
                else:
                    break

            # Fetch presence for each extension
            agents = []
            for ext in all_extensions:
                ext_id = ext.get("id")
                name = ext.get("name", "")
                ext_number = ext.get("extensionNumber", "")

                try:
                    pres_resp = requests.get(
                        f"{self.server_url}/restapi/v1.0/account/{self.account_id}/extension/{ext_id}/presence",
                        headers=self._rc_headers(),
                        timeout=10,
                    )
                    if pres_resp.ok:
                        p = pres_resp.json()
                        agents.append({
                            "ext_id": str(ext_id),
                            "ext_number": ext_number,
                            "name": name,
                            "status": p.get("presenceStatus", "Offline"),
                            "dnd_status": p.get("dndStatus", ""),
                            "telephony_status": p.get("telephonyStatus", "NoCall"),
                            "active_calls": p.get("activeCalls", []),
                        })
                except Exception:
                    agents.append({
                        "ext_id": str(ext_id),
                        "ext_number": ext_number,
                        "name": name,
                        "status": "Unknown",
                        "telephony_status": "Unknown",
                        "active_calls": [],
                    })

            log.info("RingEX: %d agent statuses fetched", len(agents))
            return agents

        except Exception as e:
            log.error("RingEX agent statuses error: %s", e)
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
