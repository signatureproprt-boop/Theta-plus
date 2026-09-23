"""Phase G — Telegram alert provider (notifications only).

SECURITY (§3/§29): the bot token and chat id are read from the environment
ONLY (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). They are never written to source,
the frontend, Sheets, logs, API responses or a chart payload. `redact()` scrubs
any credential fragment out of error strings before they are stored/logged.

If credentials are absent the provider reports NOT_CONFIGURED and the signal
engine continues untouched.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 8.0


class TelegramProvider:
    """Minimal sendMessage client. No polling, no commands, no execution."""

    name = "telegram"

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        self._token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self._token) and bool(self._chat_id)

    def redact(self, text: str) -> str:
        """Remove any credential fragment from a message/error string."""
        out = text
        for secret in (self._token, self._chat_id):
            if secret:
                out = out.replace(secret, "***REDACTED***")
                if ":" in secret:  # bot tokens are "<id>:<secret>"
                    head, _, tail = secret.partition(":")
                    for part in (head, tail):
                        if len(part) >= 6:
                            out = out.replace(part, "***REDACTED***")
        return out

    async def send(self, message: str) -> None:
        if not self.configured:
            raise RuntimeError("telegram NOT_CONFIGURED: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing")
        url = f"{API_BASE}/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                res = await client.post(
                    url,
                    json={"chat_id": self._chat_id, "text": message, "disable_web_page_preview": True},
                )
            if res.status_code >= 400:
                raise RuntimeError(f"telegram HTTP {res.status_code}: {self.redact(res.text[:200])}")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"telegram transport error: {self.redact(str(exc))}") from None
