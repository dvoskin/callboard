"""
Telegram Bot API client for the sales team group chat widget.

Provides read-only access to group chat messages and optional
send capability via a Telegram bot.

Required env vars:
  TELEGRAM_BOT_TOKEN  – Bot API token from @BotFather
  TELEGRAM_CHAT_ID    – Group chat ID (negative number for groups)
                        Use the /getUpdates trick or @userinfobot to find it

Optional:
  TELEGRAM_ALLOW_SEND – Set to "1" to allow sending messages from the dashboard
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.allow_send = os.getenv("TELEGRAM_ALLOW_SEND", "0") == "1"
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""

        # Message cache to reduce API calls
        self._messages_cache: list[dict] = []
        self._cache_expiry: float = 0
        self._CACHE_TTL = 15  # refresh every 15 seconds

        # Track the last update_id we've seen for polling
        self._last_update_id: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _api(self, method: str, params: dict = None, timeout: int = 10) -> dict:
        """Make a Telegram Bot API call."""
        resp = requests.get(
            f"{self.base_url}/{method}",
            params=params or {},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
        return data.get("result", {})

    def get_chat_info(self) -> dict:
        """Get basic info about the configured chat."""
        if not self.configured:
            return {}
        try:
            chat = self._api("getChat", {"chat_id": self.chat_id})
            return {
                "id": chat.get("id"),
                "title": chat.get("title", ""),
                "type": chat.get("type", ""),
                "photo": bool(chat.get("photo")),
            }
        except Exception as e:
            log.error("Telegram getChat error: %s", e)
            return {}

    def get_recent_messages(self, limit: int = 50) -> list[dict]:
        """Fetch recent messages from the group chat.

        Uses getUpdates with allowed_updates=["message"] to get new messages,
        then merges with cache. Falls back to cached data on error.

        Note: getUpdates only returns messages received AFTER the bot was added
        and only keeps ~24h of updates. For persistent history, consider
        storing messages in a local DB.
        """
        if not self.configured:
            return []

        # Return cache if fresh
        if self._messages_cache and time.time() < self._cache_expiry:
            return self._messages_cache[:limit]

        try:
            # Use getUpdates to poll for new messages
            params = {
                "allowed_updates": '["message"]',
                "timeout": 1,
            }
            if self._last_update_id:
                params["offset"] = self._last_update_id + 1

            updates = self._api("getUpdates", params, timeout=5)

            new_messages = []
            for update in updates:
                self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                msg = update.get("message")
                if not msg:
                    continue
                # Only include messages from our target chat
                if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                    continue
                parsed = self._parse_message(msg)
                if parsed:
                    new_messages.append(parsed)

            # Merge new messages into cache (avoid duplicates by message_id)
            existing_ids = {m["id"] for m in self._messages_cache}
            for m in new_messages:
                if m["id"] not in existing_ids:
                    self._messages_cache.append(m)

            # Sort by timestamp and keep last 200
            self._messages_cache.sort(key=lambda m: m["timestamp"])
            self._messages_cache = self._messages_cache[-200:]

            self._cache_expiry = time.time() + self._CACHE_TTL
            return self._messages_cache[:limit]

        except Exception as e:
            log.error("Telegram getUpdates error: %s", e)
            return self._messages_cache[:limit]

    def _parse_message(self, msg: dict) -> Optional[dict]:
        """Parse a raw Telegram message into a simplified dict."""
        from_user = msg.get("from") or {}
        text = msg.get("text") or ""

        # Handle different message types
        if not text:
            if msg.get("photo"):
                text = "[Photo]"
            elif msg.get("document"):
                text = f"[File: {msg['document'].get('file_name', 'document')}]"
            elif msg.get("voice"):
                text = "[Voice message]"
            elif msg.get("video"):
                text = "[Video]"
            elif msg.get("sticker"):
                text = f"[Sticker: {msg['sticker'].get('emoji', '')}]"
            elif msg.get("location"):
                text = "[Location]"
            elif msg.get("contact"):
                text = f"[Contact: {msg['contact'].get('first_name', '')}]"
            elif msg.get("new_chat_members"):
                names = ", ".join(u.get("first_name", "?") for u in msg["new_chat_members"])
                text = f"{names} joined the group"
            elif msg.get("left_chat_member"):
                text = f"{msg['left_chat_member'].get('first_name', '?')} left the group"
            else:
                return None  # Skip unsupported message types

        # Handle reply
        reply_to = None
        if msg.get("reply_to_message"):
            reply_msg = msg["reply_to_message"]
            reply_from = reply_msg.get("from") or {}
            reply_to = {
                "name": (reply_from.get("first_name") or "") + " " + (reply_from.get("last_name") or ""),
                "text": (reply_msg.get("text") or "")[:80],
            }

        timestamp = msg.get("date", 0)
        return {
            "id": msg.get("message_id"),
            "text": text,
            "from_name": ((from_user.get("first_name") or "") + " " + (from_user.get("last_name") or "")).strip(),
            "from_username": from_user.get("username", ""),
            "from_id": from_user.get("id"),
            "is_bot": from_user.get("is_bot", False),
            "timestamp": timestamp,
            "time_iso": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat() if timestamp else "",
            "reply_to": reply_to,
        }

    def send_message(self, text: str, sender_name: str = "", reply_to_message_id: int = None) -> Optional[dict]:
        """Send a message to the group chat via the bot.

        Messages are prefixed with the sender's name so the team knows
        who is writing from the dashboard.
        Only works if TELEGRAM_ALLOW_SEND=1 is set.
        """
        if not self.configured:
            raise RuntimeError("Telegram not configured")
        if not self.allow_send:
            raise RuntimeError("Sending disabled — set TELEGRAM_ALLOW_SEND=1")

        # Prefix with sender name so the group knows who's talking
        display_text = f"<b>{sender_name}</b>: {text}" if sender_name else text

        params = {
            "chat_id": self.chat_id,
            "text": display_text,
            "parse_mode": "HTML",
        }
        if reply_to_message_id:
            params["reply_to_message_id"] = reply_to_message_id

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                msg = self._parse_message(data["result"])
                if msg:
                    self._messages_cache.append(msg)
                return msg
            raise RuntimeError(data.get("description", "Send failed"))
        except requests.exceptions.RequestException as e:
            log.error("Telegram send error: %s", e)
            raise RuntimeError(f"Failed to send: {e}")
