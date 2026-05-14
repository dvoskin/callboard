"""
RingCX / RingCentral live monitoring client.

Provides real-time visibility into active calls and agent statuses
via the RingCentral Platform API.

Required env vars:
  RC_CLIENT_ID       – OAuth app client ID (from developers.ringcentral.com) 
  RC_CLIENT_SECRET   – OAuth app client secret
  RC_JWT_TOKEN       – JWT credential for server-to-server auth
  RC_ACCOUNT_ID      – RingCentral account ID (default: "~" for current)

Scopes needed on the RC app:
  - ReadPresence       (agent statuses)
  - ReadCallLog        (recent calls)
  - ReadTelephonySessions  (active calls)
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
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.jwt_token)

    # ── Auth ──

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

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
        self._access_token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600) - 60
        log.info("RingCX auth token acquired (expires in %ds)", data.get("expires_in", 0))
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # ── Active Calls ──

    def get_active_calls(self) -> list[dict]:
        """Fetch currently active telephony sessions across the account.

        Returns a list of simplified call objects:
        [
            {
                "session_id": "...",
                "status": "InProgress" | "Ringing" | "Hold" | ...,
                "direction": "Inbound" | "Outbound",
                "from": "+15551234567",
                "to": "+15559876543",
                "agent_name": "Jane Doe",
                "agent_ext": "101",
                "started_at": "2026-05-14T10:30:00Z",
                "duration_sec": 125,
            },
            ...
        ]
        """
        try:
            resp = requests.get(
                f"{self.server_url}/restapi/v1.0/account/{self.account_id}/telephony/sessions",
                headers=self._headers(),
                params={"perPage": 100},
                timeout=15,
            )
            if resp.status_code == 204:
                return []
            resp.raise_for_status()
            sessions = resp.json().get("records", [])

            active = []
            now = datetime.now(timezone.utc)
            for session in sessions:
                for party in session.get("parties", []):
                    status = party.get("status", {}).get("code", "")
                    if status in ("Disconnected", "Gone"):
                        continue
                    direction = party.get("direction", "")
                    from_info = party.get("from", {})
                    to_info = party.get("to", {})
                    ext_info = party.get("extensionId")

                    # Parse start time
                    created = session.get("creationTime") or ""
                    started = None
                    dur = 0
                    if created:
                        try:
                            started = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            dur = int((now - started).total_seconds())
                        except Exception:
                            pass

                    active.append({
                        "session_id": session.get("id"),
                        "status": status,
                        "direction": direction,
                        "from": from_info.get("phoneNumber", from_info.get("name", "")),
                        "to": to_info.get("phoneNumber", to_info.get("name", "")),
                        "agent_name": party.get("name", ""),
                        "agent_ext": str(party.get("extensionId", "")),
                        "started_at": created,
                        "duration_sec": max(0, dur),
                    })

            log.info("RingCX: %d active call parties found", len(active))
            return active

        except requests.exceptions.HTTPError as e:
            log.error("RingCX active calls error: %s", e)
            raise
        except Exception as e:
            log.error("RingCX active calls error: %s", e)
            return []

    # ── Agent Presence / Extensions ──

    def get_agent_statuses(self) -> list[dict]:
        """Fetch presence status for all extensions.

        Returns:
        [
            {
                "ext_id": "12345",
                "name": "Jane Doe",
                "status": "Available" | "Busy" | "DoNotDisturb" | "Offline",
                "telephony_status": "NoCall" | "Ringing" | "OnHold" | "CallConnected",
                "ring_on_monitored": False,
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
                    headers=self._headers(),
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

            # Now fetch presence for each extension (batch where possible)
            agents = []
            for ext in all_extensions:
                ext_id = ext.get("id")
                name = ext.get("name", "")
                ext_number = ext.get("extensionNumber", "")

                try:
                    pres_resp = requests.get(
                        f"{self.server_url}/restapi/v1.0/account/{self.account_id}/extension/{ext_id}/presence",
                        headers=self._headers(),
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

            log.info("RingCX: %d agent statuses fetched", len(agents))
            return agents

        except Exception as e:
            log.error("RingCX agent statuses error: %s", e)
            return []

    # ── Combined live snapshot ──

    def get_live_snapshot(self) -> dict:
        """Return a combined live monitoring snapshot."""
        active_calls = self.get_active_calls()
        agents = self.get_agent_statuses()

        # Summarize
        on_call = [a for a in agents if a["telephony_status"] in ("CallConnected", "Ringing", "OnHold")]
        available = [a for a in agents if a["status"] == "Available" and a["telephony_status"] == "NoCall"]
        dnd = [a for a in agents if a["dnd_status"] == "DoNotAcceptAnyCalls" or a["status"] == "DoNotDisturb"]

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "active_calls": active_calls,
            "agents": agents,
            "summary": {
                "total_agents": len(agents),
                "on_call": len(on_call),
                "available": len(available),
                "dnd": len(dnd),
                "active_call_count": len(active_calls),
            },
        }
