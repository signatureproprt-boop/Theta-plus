"""Phase A — READ-ONLY Dhan market-data client.

SECURITY / SCOPE CONTRACT (V1, signal-only):
  * This class contains NO order-placement, modification, cancellation or
    position methods — by design, not by omission. V1 output is CE_SETUP /
    PE_SETUP / WAIT only.
  * Credentials come exclusively from the environment (DHAN_ACCESS_TOKEN /
    DHAN_CLIENT_ID in backend/.env). They are never hardcoded or logged.

All network failures are wrapped in DhanDataError so one bad response can
never crash the strategy process (master prompt §39).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime
from typing import Any, Optional

import httpx

NIFTY_INDEX_SECURITY_ID = "13"  # Dhan security id for NIFTY 50 index (IDX_I)
NIFTY_UNDERLYING_SEGMENT = "IDX_I"

# Official DhanHQ v2 rate limits (docs: Market Quote = 1 req/s,
# Option Chain = 1 unique req / 3 s). Respected client-side so a live run never
# gets throttled into stale data.
MIN_INTERVAL_SECONDS = {"marketfeed": 1.05, "optionchain": 3.05}


class DhanDataError(RuntimeError):
    """Any failure to fetch market data (timeout, non-2xx, bad payload)."""


class DhanMarketData:
    """Thin async wrapper over the read-only Dhan market-data endpoints."""

    BASE_URL = "https://api.dhan.co"

    def __init__(
        self,
        access_token: Optional[str] = None,
        client_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self._access_token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        self._client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self._timeout = timeout
        self._last_call: dict[str, float] = {}
        self._expiries: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        """True when credentials are present in the environment."""
        return bool(self._access_token and self._client_id)

    def _headers(self) -> dict[str, str]:
        if not self.configured:
            raise DhanDataError(
                "Dhan credentials missing: set DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID in backend/.env"
            )
        return {
            "access-token": self._access_token,
            "client-id": self._client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _throttle(self, family: str) -> None:
        wait = MIN_INTERVAL_SECONDS.get(family, 0.0)
        if not wait:
            return
        last = self._last_call.get(family, 0.0)
        delta = time.monotonic() - last
        if delta < wait:
            await asyncio.sleep(wait - delta)
        self._last_call[family] = time.monotonic()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self._throttle("optionchain" if "optionchain" in path else "marketfeed")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                resp = await http.post(f"{self.BASE_URL}{path}", json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise DhanDataError(f"Dhan request failed ({path}): {exc}") from exc
        if resp.status_code >= 400:
            raise DhanDataError(f"Dhan returned {resp.status_code} for {path}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise DhanDataError(f"Dhan returned non-JSON body for {path}") from exc
        if not isinstance(data, dict):
            raise DhanDataError(f"Dhan returned an unexpected payload for {path}")
        return data

    async def get_index_quote(self, security_id: str = NIFTY_INDEX_SECURITY_ID) -> dict[str, Any]:
        """NIFTY index snapshot with OHLC + volume + last_trade_time.

        Uses POST /v2/marketfeed/quote (verified against the current official
        DhanHQ v2 "Market Quote / Market Depth Data" contract) — the /ltp
        endpoint returns last_price ONLY and cannot feed the VWAP engine.
        """
        return await self._post(
            "/v2/marketfeed/quote",
            {NIFTY_UNDERLYING_SEGMENT: [int(security_id)]},
        )

    async def get_index_ltp(self, security_id: str = NIFTY_INDEX_SECURITY_ID) -> dict[str, Any]:
        """Lightweight LTP-only snapshot (POST /v2/marketfeed/ltp)."""
        return await self._post(
            "/v2/marketfeed/ltp",
            {NIFTY_UNDERLYING_SEGMENT: [int(security_id)]},
        )

    async def get_expiry_list(self, security_id: str = NIFTY_INDEX_SECURITY_ID) -> list[str]:
        """Active option expiries for the underlying (POST /v2/optionchain/expirylist).

        Never guessed: the expiry used for the option chain always comes from here.
        """
        payload = await self._post(
            "/v2/optionchain/expirylist",
            {"UnderlyingScrip": int(security_id), "UnderlyingSeg": NIFTY_UNDERLYING_SEGMENT},
        )
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise DhanDataError("Dhan returned no active expiries for the underlying")
        self._expiries = tuple(str(d) for d in data)
        return list(self._expiries)

    async def get_nearest_expiry(
        self, on: Optional[date] = None, security_id: str = NIFTY_INDEX_SECURITY_ID
    ) -> str:
        """Nearest non-past expiry from the official expiry list."""
        today = on or datetime.now().date()
        expiries = list(self._expiries) or await self.get_expiry_list(security_id)
        future = [e for e in sorted(expiries) if e >= today.isoformat()]
        if not future:
            raise DhanDataError("no non-expired option expiry available from Dhan")
        return future[0]

    async def get_option_chain(
        self, expiry: str, security_id: str = NIFTY_INDEX_SECURITY_ID
    ) -> dict[str, Any]:
        """Full NIFTY option chain for one expiry.

        Request body follows the current official DhanHQ v2 contract exactly:
        {"UnderlyingScrip": <int>, "UnderlyingSeg": "IDX_I", "Expiry": "YYYY-MM-DD"}.
        Response is {"data": {"last_price": .., "oc": {"<strike>": {"ce": {..}, "pe": {..}}}}}.
        """
        if not expiry:
            expiry = await self.get_nearest_expiry(security_id=security_id)
        return await self._post(
            "/v2/optionchain",
            {
                "UnderlyingScrip": int(security_id),
                "UnderlyingSeg": NIFTY_UNDERLYING_SEGMENT,
                "Expiry": expiry,
            },
        )

    async def get_index_minute_candles(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Broker OHLCV candles; a daily quote is never a substitute for VWAP bars."""
        return await self._post("/v2/charts/intraday", {
            "securityId": NIFTY_INDEX_SECURITY_ID,
            "exchangeSegment": NIFTY_UNDERLYING_SEGMENT,
            "instrument": "INDEX", "interval": "1", "oi": False,
            "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": end.strftime("%Y-%m-%d %H:%M:%S"),
        })
