"""Phase H — execution/risk models (ADDITIVE layer; strategy stays frozen).

Field names and enum values mirror the CURRENT official DhanHQ v2 contract
verified against https://dhanhq.co/docs/v2/orders/ and /portfolio/ :

  POST   /v2/orders                         -> {orderId, orderStatus}
  GET    /v2/orders/{order-id}
  GET    /v2/orders/external/{correlation-id}
  GET    /v2/orders                          (order book)
  GET    /v2/positions
  header: access-token: <JWT>

Broker order statuses: TRANSIT, PENDING, REJECTED, CANCELLED, PART_TRADED,
TRADED, EXPIRED. They are mapped (never equated) to our internal lifecycle.

DEFAULT SAFETY: live execution is OFF and DISARMED; nothing here can submit an
order by itself — only `execution.service.ExecutionService` may, and only after
`execution.gate.can_execute_order` approves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

# --- modes -----------------------------------------------------------------
MODE_RESEARCH = "RESEARCH"
MODE_PAPER = "PAPER"
MODE_LIVE = "LIVE"

# --- internal order lifecycle (§32) ---------------------------------------
ORDER_REQUESTED = "REQUESTED"
ORDER_SUBMITTED = "SUBMITTED"
ORDER_ACKNOWLEDGED = "ACKNOWLEDGED"
ORDER_PARTIALLY_FILLED = "PARTIALLY_FILLED"
ORDER_FILLED = "FILLED"
ORDER_REJECTED = "REJECTED"
ORDER_CANCELLED = "CANCELLED"
ORDER_UNKNOWN = "UNKNOWN"

# Dhan orderStatus -> internal lifecycle (SUBMITTED is never FILLED)
BROKER_STATUS_MAP = {
    "TRANSIT": ORDER_SUBMITTED,
    "PENDING": ORDER_ACKNOWLEDGED,
    "PART_TRADED": ORDER_PARTIALLY_FILLED,
    "TRADED": ORDER_FILLED,
    "REJECTED": ORDER_REJECTED,
    "CANCELLED": ORDER_CANCELLED,
    "EXPIRED": ORDER_CANCELLED,
}

# --- deterministic block reasons (§48) ------------------------------------
APPROVED = "EXECUTION_APPROVED"
BLOCK_MODE_NOT_LIVE = "MODE_NOT_LIVE"
BLOCK_LIVE_DISABLED = "LIVE_EXECUTION_DISABLED"
BLOCK_NOT_ARMED = "LIVE_EXECUTION_NOT_ARMED"
BLOCK_KILL_SWITCH = "KILL_SWITCH"
BLOCK_SYSTEM_DISABLED = "SYSTEM_DISABLED"
BLOCK_INVALID_SIGNAL = "INVALID_SIGNAL"
BLOCK_SIGNAL_STATE = "SIGNAL_STATE_NOT_ENTRY"
BLOCK_STALE_DATA = "STALE_DATA"
BLOCK_SESSION = "OUTSIDE_TRADING_SESSION"
BLOCK_COOLDOWN = "COOLDOWN"
BLOCK_DUPLICATE = "DUPLICATE_ORDER"
BLOCK_MAX_TRADES = "MAX_TRADES_PER_DAY"
BLOCK_MAX_POSITIONS = "MAX_OPEN_POSITIONS"
BLOCK_QUANTITY = "QUANTITY_LIMIT"
BLOCK_MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
BLOCK_SLIPPAGE = "SLIPPAGE_LIMIT"
BLOCK_PRICE_INVALID = "PRICE_INVALID"
BLOCK_SECURITY_ID = "SECURITY_ID_UNAVAILABLE"
BLOCK_INSTRUMENT = "INSTRUMENT_VALIDATION_FAILED"
BLOCK_RECONCILIATION = "RECONCILIATION_MISMATCH"
BLOCK_UNRECONCILED_POSITION = "UNRECONCILED_POSITION"
BLOCK_RISK_DISABLED = "RISK_ENGINE_DISABLED"
BLOCK_CONTROL_DISABLED = "LIVE_EXECUTION_CONTROL_DISABLED"

# --- reconciliation states -------------------------------------------------
RECON_OK = "OK"
RECON_PENDING = "PENDING"
RECON_MISMATCH = "MISMATCH"


class InstrumentRef(BaseModel):
    """A validated, mapped tradable instrument. Never constructed by guesswork."""

    model_config = ConfigDict(frozen=True)

    underlying: str = "NIFTY"
    exchange_segment: str = "NSE_FNO"  # Dhan annexure value for NSE F&O
    security_id: str
    trading_symbol: str = ""
    expiry: str = ""
    strike: Optional[int] = None
    option_type: str = ""  # CALL | PUT (Dhan drvOptionType vocabulary)
    lot_size: int = 0
    tradable: bool = False

    @field_validator("underlying")
    @classmethod
    def _nifty_only(cls, v: str) -> str:
        if v != "NIFTY":
            raise ValueError(f"instrument {v!r} is out of scope: NIFTY only")
        return v


class OrderRequest(BaseModel):
    """Immutable, validated broker order request (Dhan v2 field vocabulary).

    Built ONLY by the execution service from backend-verified values; an
    arbitrary frontend payload can never become an order (there is no POST
    endpoint that accepts one).
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str  # our idempotency key; sent as Dhan correlationId
    signal_id: str
    instrument: str = "NIFTY"

    exchange_segment: str = "NSE_FNO"
    security_id: str
    transaction_type: str = "BUY"  # BUY | SELL
    order_type: str = "LIMIT"  # LIMIT | MARKET | STOP_LOSS | STOP_LOSS_MARKET
    product_type: str = "INTRADAY"  # CNC | INTRADAY | MARGIN | MTF | CO | BO
    validity: str = "DAY"  # DAY | IOC
    quantity: int
    price: float = 0.0
    trigger_price: Optional[float] = None
    disclosed_quantity: int = 0
    after_market_order: bool = False

    reference_price: Optional[float] = None  # signal-time price for slippage checks
    mode: str = MODE_LIVE

    @field_validator("transaction_type")
    @classmethod
    def _side(cls, v: str) -> str:
        if v not in ("BUY", "SELL"):
            raise ValueError(f"transactionType must be BUY or SELL (got {v!r})")
        return v

    @field_validator("order_type")
    @classmethod
    def _otype(cls, v: str) -> str:
        allowed = ("LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_MARKET")
        if v not in allowed:
            raise ValueError(f"orderType must be one of {allowed} (got {v!r})")
        return v

    @field_validator("product_type")
    @classmethod
    def _ptype(cls, v: str) -> str:
        allowed = ("CNC", "INTRADAY", "MARGIN", "MTF", "CO", "BO")
        if v not in allowed:
            raise ValueError(f"productType must be one of {allowed} (got {v!r})")
        return v

    @field_validator("validity")
    @classmethod
    def _validity(cls, v: str) -> str:
        if v not in ("DAY", "IOC"):
            raise ValueError(f"validity must be DAY or IOC (got {v!r})")
        return v

    @field_validator("security_id")
    @classmethod
    def _security(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("securityId is required and must come from a validated mapping")
        return v

    @field_validator("quantity")
    @classmethod
    def _qty(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"quantity must be > 0 (got {v})")
        return v

    def to_dhan_payload(self, dhan_client_id: str) -> dict:
        """Exact DhanHQ v2 POST /orders body (verified field names)."""
        payload = {
            "dhanClientId": dhan_client_id,
            "correlationId": self.client_order_id[:30],
            "transactionType": self.transaction_type,
            "exchangeSegment": self.exchange_segment,
            "productType": self.product_type,
            "orderType": self.order_type,
            "validity": self.validity,
            "securityId": self.security_id,
            "quantity": self.quantity,
            "disclosedQuantity": self.disclosed_quantity,
            "price": self.price,
            "afterMarketOrder": self.after_market_order,
        }
        if self.trigger_price is not None:
            payload["triggerPrice"] = self.trigger_price
        return payload


class OrderRecord(BaseModel):
    """Internal order state + broker-confirmed facts. No credentials stored."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    signal_id: str
    broker_order_id: Optional[str] = None
    broker_status: Optional[str] = None  # raw Dhan orderStatus
    status: str = ORDER_REQUESTED  # internal lifecycle
    mode: str = MODE_LIVE

    instrument: str = "NIFTY"
    security_id: str = ""
    exchange_segment: str = "NSE_FNO"
    transaction_type: str = "BUY"
    order_type: str = "LIMIT"

    requested_quantity: int = 0
    filled_quantity: int = 0
    remaining_quantity: int = 0
    requested_price: Optional[float] = None
    average_fill_price: Optional[float] = None

    created_at: datetime
    updated_at: datetime
    error_code: str = ""
    error_message: str = ""
    reconciliation_state: str = RECON_PENDING


class RiskDecision(BaseModel):
    """Deterministic risk verdict. No AI, no probability, no capital inference."""

    model_config = ConfigDict(frozen=True)

    approved: bool
    reason: str  # APPROVED or one of the BLOCK_* constants
    detail: str = ""
    checks: tuple[tuple[str, bool], ...] = ()
    trades_today: int = 0
    open_positions: int = 0
    realized_pnl_today: float = 0.0


class ExecutionDecision(BaseModel):
    """Output of the single execution gate."""

    model_config = ConfigDict(frozen=True)

    approved: bool
    reason: str
    detail: str = ""
    checks: tuple[tuple[str, bool], ...] = ()
    client_order_id: Optional[str] = None
    signal_id: Optional[str] = None
    mode: str = MODE_RESEARCH


class ExecutionLogEntry(BaseModel):
    """EXECUTION_LOG row (§47). Never contains secrets."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    event: str
    signal_id: str = ""
    client_order_id: str = ""
    broker_order_id: str = ""
    mode: str = MODE_RESEARCH
    instrument: str = "NIFTY"
    security_id: str = ""
    transaction_type: str = ""
    order_type: str = ""
    quantity: int = 0
    requested_price: Optional[float] = None
    average_fill_price: Optional[float] = None
    status: str = ""
    risk_decision: str = ""
    block_reason: str = ""
    reconciliation_state: str = RECON_PENDING
    error_code: str = ""
    message: str = ""


class BrokerPosition(BaseModel):
    """Broker-confirmed position (subset of Dhan GET /v2/positions)."""

    model_config = ConfigDict(frozen=True)

    security_id: str
    trading_symbol: str = ""
    exchange_segment: str = ""
    product_type: str = ""
    position_type: str = ""  # LONG | SHORT | CLOSED
    net_qty: int = 0
    buy_avg: float = 0.0
    realized_profit: float = 0.0
    unrealized_profit: float = 0.0


class ReconciliationReport(BaseModel):
    """Internal vs broker comparison. Broker state is authoritative (§37)."""

    model_config = ConfigDict(frozen=True)

    state: str = RECON_PENDING
    checked_at: Optional[datetime] = None
    internal_open_positions: int = 0
    broker_open_positions: int = 0
    unexpected_broker_positions: tuple[BrokerPosition, ...] = ()
    detail: str = ""
    blocks_execution: bool = True  # PENDING blocks until a successful reconcile


class ExecutionStatusPanel(BaseModel):
    """Read-only live-control-panel payload (§44). No broker secrets."""

    model_config = ConfigDict(frozen=True)

    system_mode: str = MODE_RESEARCH
    live_execution_enabled: bool = False
    live_execution_armed: bool = False
    kill_switch: bool = False
    control_enabled: bool = False  # server-side authorization present?
    control_note: str = ""

    risk_state: str = "PASS"  # PASS | BLOCKED
    risk_reason: str = ""
    broker: str = "DISCONNECTED"  # CONNECTED | DISCONNECTED | NOT_CONFIGURED
    broker_verified: bool = False
    reconciliation: str = RECON_PENDING
    adapter: str = "mock"  # mock | dhan

    max_trades_per_day: int = 0
    max_open_positions: int = 0
    max_quantity: int = 0
    max_daily_loss: float = 0.0
    max_slippage_points: float = 0.0
    quantity: int = 0

    trades_today: int = 0
    open_orders: int = 0
    realized_pnl_today: float = 0.0
    last_block_reason: str = ""
    orders_submitted: int = 0
    confirmation_required: tuple[str, ...] = ()
    note: str = ""
