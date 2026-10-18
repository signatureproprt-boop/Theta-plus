"""Phase D — Google Sheets transport adapter (env-gated, failure-tolerant).

Sheet structure (created/written by writers.py):
  DASHBOARD, SETTINGS, RAW_DATA, PCR_DATA, SIGNAL, TRADE_LOG, SYSTEM_LOG

Safety contract:
  * Adapter is DISABLED unless GOOGLE_SHEETS_ID + GOOGLE_SERVICE_ACCOUNT_JSON
    (inline JSON or a file path) exist in the environment — the signal engine
    must stay fully functional without it.
  * Every call is timeout-bounded, retried with backoff on 429/5xx, and any
    failure raises SheetsError with REDACTED messages (no tokens/keys ever).
  * Batching is the caller's job (sheets/writers.py) — one HTTP call per tab
    per flush, never per row.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import httpx
import jwt

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
BASE = "https://sheets.googleapis.com/v4/spreadsheets"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SheetsError(RuntimeError):
    """Any Google Sheets failure after retries. Message is always redacted."""


def redact(text: str, *secrets: str) -> str:
    """Remove any credential material from a message before it can be logged."""
    out = text
    for secret in (s for s in secrets if s):
        out = out.replace(secret, "[REDACTED]")
    return re.sub(r"-----BEGIN[^-]+PRIVATE KEY-----[\s\S]*?-----END[^-]+PRIVATE KEY-----", "[REDACTED-KEY]", out)


def load_service_account(spec: Optional[str]) -> dict[str, Any]:
    """Service-account JSON from an inline env value or a file path. Never logged."""
    if not spec:
        return {}
    try:
        if spec.lstrip().startswith("{"):
            data = json.loads(spec)
        else:
            with open(os.path.expanduser(spec), "r", encoding="utf-8") as fh:
                data = json.load(fh)
    except Exception as exc:  # bad JSON / missing file
        raise SheetsError(f"invalid GOOGLE_SERVICE_ACCOUNT_JSON: {redact(str(exc))}") from exc
    if not isinstance(data, dict) or "client_email" not in data or "private_key" not in data:
        raise SheetsError("service-account JSON missing client_email/private_key")
    return data


class GoogleSheetsAdapter:
    """Thin async Sheets v4 client. Batching, retries and rate-limit handling built in."""

    def __init__(
        self,
        spreadsheet_id: Optional[str] = None,
        service_account_spec: Optional[str] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id or os.environ.get("GOOGLE_SHEETS_ID", "")
        try:
            self._service_account = load_service_account(
                service_account_spec or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
            )
        except SheetsError:
            self._service_account = {}
            raise
        self._timeout = timeout
        self._max_retries = max_retries
        self._transport = transport
        self._token: str = ""
        self._token_expiry: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.spreadsheet_id and self._service_account)

    @property
    def enabled(self) -> bool:
        return self.configured

    async def _access_token(self) -> str:
        """Service-account JWT -> OAuth2 access token, cached until near-expiry."""
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        issued = int(now)
        assertion = jwt.encode(
            {
                "iss": self._service_account["client_email"],
                "scope": SCOPE,
                "aud": TOKEN_URL,
                "iat": issued,
                "exp": issued + 3600,
            },
            self._service_account["private_key"],
            algorithm="RS256",
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                resp = await http.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
        except httpx.HTTPError as exc:
            raise SheetsError(f"google token request failed: {redact(str(exc))}") from exc
        if resp.status_code != 200:
            raise SheetsError(f"google token request returned {resp.status_code}")
        payload = resp.json()
        self._token = payload.get("access_token", "")
        self._token_expiry = now + float(payload.get("expires_in", 0))
        if not self._token:
            raise SheetsError("google token response missing access_token")
        return self._token

    async def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        """Single rate-limited, retrying request. Raises redacted SheetsError."""
        token = await self._access_token()
        last = "unknown error"
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                    resp = await http.request(
                        method,
                        f"{BASE}/{self.spreadsheet_id}{path}",
                        params=params,
                        json=json_body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError as exc:
                last = f"request failed: {redact(str(exc))}"
            else:
                if resp.status_code < 300:
                    return resp.json() if resp.content else {}
                last = f"google returned {resp.status_code}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise SheetsError(f"google sheets {method} {path}: {last}")
            await __import__("asyncio").sleep(min(2 ** attempt, 5))
        raise SheetsError(f"google sheets {method} {path}: {last} (after {self._max_retries} attempts)")

    async def append_rows(self, tab: str, rows: Sequence[Sequence[Any]]) -> int:
        """Append a BATCH of rows to one tab — a single HTTP call for all rows."""
        if not rows:
            return 0
        await self._request(
            "POST",
            f"/values/{tab}:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json_body={"values": [list(r) for r in rows]},
        )
        return len(rows)

    async def write_grid(self, tab: str, grid: Sequence[Sequence[Any]]) -> int:
        """Overwrite a tab (used for DASHBOARD/SETTINGS mirrors): clear, then one update."""
        await self._request("POST", f"/values/{tab}:clear")
        await self._request(
            "PUT",
            f"/values/{tab}!A1",
            params={"valueInputOption": "RAW"},
            json_body={"values": [list(r) for r in grid]},
        )
        return len(grid)

    async def read_key_values(self, tab: str) -> dict[str, str]:
        """Read a KEY/VALUE two-column tab (SETTINGS) into a dict."""
        data = await self._request("GET", f"/values/{tab}!A1:B1000")
        out: dict[str, str] = {}
        for row in data.get("values", []):
            if len(row) >= 2 and str(row[0]).strip():
                out[str(row[0]).strip()] = str(row[1]).strip()
        return out


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
