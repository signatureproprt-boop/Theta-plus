"""Phase H — DhanExecutionAdapter: the ONE module allowed to call Dhan order APIs.

Verified against the current official DhanHQ v2 documentation
(https://dhanhq.co/docs/v2/orders/ and /portfolio/, retrieved for this build):

    POST   https://api.dhan.co/v2/orders                       place order
    GET    https://api.dhan.co/v2/orders/{order-id}            order status
    GET    https://api.dhan.co/v2/orders/external/{corr-id}     status by correlationId
    GET    https://api.dhan.co/v2/orders                        order book
    GET    https://api.dhan.co/v2/positions                     open positions
    headers: 'access-token: <JWT>', 'Content-Type: application/json'

Notes carried from the documentation:
- Order placement/modification/cancellation requires STATIC IP WHITELISTING on
  the Dhan side; without it live submission will fail at the broker.
- orderStatus vocabulary: TRANSIT / PENDING / REJECTED / CANCELLED /
  PART_TRADED / TRADED / EXPIRED.
- `correlationId` is max 30 chars and is used here as our idempotency key so a
  timeout can be reconciled through /orders/external/{correlation-id}.

SECURITY: credentials come ONLY from the environment (DHAN_ACCESS_TOKEN,
DHAN_CLIENT_ID); they are never logged, returned or stored. `redact()` scrubs
them from every error string. Market data stays in `data/dhan_client.py`:
this adapter performs execution only, the two are never combined.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

from execution.adapter import (
    BrokerOrderAck,
    BrokerOrderState,
    ExecutionError,
    ExecutionTimeout,
)
from models.execution_models import BrokerPosition, OrderRequest

API_BASE = "https://api.dhan.co/v2"
TIMEOUT_SECONDS = 10.0


class DhanExecutionAdapter:
    """Live broker execution. Inert unless credentials are configured."""

    name = "dhan"

    def __init__(self, access_token: Optional[str] = None, client_id: Optional[str] = None) -> None:
        self._token = access_token if access_token is not None else os.environ.get("DHAN_ACCESS_TOKEN", "")
        self._client_id = client_id if client_id is not None else os.environ.get("DHAN_CLIENT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self._token) and bool(self._client_id)

    def redact(self, text: str) -> str:
        out = text
        for secret in (self._token, self._client_id):
            if secret and len(secret) >= 4:
                out = out.replace(secret, "***REDACTED***")
        return out

    def _headers(self) -> dict:
        return {"Content-Type": "application/json", "access-token": self._token}

    def _require_credentials(self) -> None:
        if not self.configured:
            raise ExecutionError(
                "dhan execution NOT_CONFIGURED: DHAN_ACCESS_TOKEN/DHAN_CLIENT_ID missing"
            )

    async def _request(self, method: str, path: str, json: Optional[dict] = None):
        self._require_credentials()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                res = await client.request(method, f"{API_BASE}{path}", headers=self._headers(), json=json)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # Ambiguous: never resend blindly — the caller reconciles (§34/§35).
            raise ExecutionTimeout(self.redact(f"dhan transport timeout: {exc}")) from None
        except httpx.HTTPError as exc:
            raise ExecutionError(self.redact(f"dhan transport error: {exc}")) from None
        if res.status_code >= 400:
            raise ExecutionError(self.redact(f"dhan HTTP {res.status_code}: {res.text[:300]}"))
        if res.status_code == 202 or not res.content:
            return {}
        return res.json()

    # --- execution operations ---
    async def submit_order(self, order: OrderRequest) -> BrokerOrderAck:
        body = await self._request("POST", "/orders", json=order.to_dhan_payload(self._client_id))
        broker_id = str(body.get("orderId", "") or "")
        status = str(body.get("orderStatus", "") or "")
        if not broker_id:
            # Ambiguous acknowledgement: treat as UNKNOWN, reconcile by correlationId.
            raise ExecutionTimeout("dhan response contained no orderId; reconciliation required")
        return BrokerOrderAck(broker_order_id=broker_id, broker_status=status)

    def _state(self, body: dict) -> BrokerOrderState:
        return BrokerOrderState(
            broker_order_id=str(body.get("orderId", "") or ""),
            broker_status=str(body.get("orderStatus", "") or ""),
            quantity=int(body.get("quantity") or 0),
            filled_quantity=int(body.get("filledQty") or 0),
            remaining_quantity=int(body.get("remainingQuantity") or 0),
            average_traded_price=(
                float(body["averageTradedPrice"]) if body.get("averageTradedPrice") else None
            ),
            correlation_id=str(body.get("correlationId", "") or ""),
            error_code=str(body.get("omsErrorCode", "") or ""),
            error_message=str(body.get("omsErrorDescription", "") or ""),
        )

    async def get_order(self, broker_order_id: str) -> BrokerOrderState:
        body = await self._request("GET", f"/orders/{broker_order_id}")
        if isinstance(body, list):
            body = body[0] if body else {}
        return self._state(body)

    async def get_order_by_correlation(self, client_order_id: str) -> Optional[BrokerOrderState]:
        try:
            body = await self._request("GET", f"/orders/external/{client_order_id[:30]}")
        except ExecutionError:
            return None
        if isinstance(body, list):
            body = body[0] if body else {}
        if not body:
            return None
        return self._state(body)

    async def get_order_history(self) -> list[BrokerOrderState]:
        body = await self._request("GET", "/orders")
        rows = body if isinstance(body, list) else []
        return [self._state(row) for row in rows]

    async def get_positions(self) -> list[BrokerPosition]:
        body = await self._request("GET", "/positions")
        rows = body if isinstance(body, list) else []
        return [
            BrokerPosition(
                security_id=str(row.get("securityId", "") or ""),
                trading_symbol=str(row.get("tradingSymbol", "") or ""),
                exchange_segment=str(row.get("exchangeSegment", "") or ""),
                product_type=str(row.get("productType", "") or ""),
                position_type=str(row.get("positionType", "") or ""),
                net_qty=int(row.get("netQty") or 0),
                buy_avg=float(row.get("buyAvg") or 0.0),
                realized_profit=float(row.get("realizedProfit") or 0.0),
                unrealized_profit=float(row.get("unrealizedProfit") or 0.0),
            )
            for row in rows
        ]
