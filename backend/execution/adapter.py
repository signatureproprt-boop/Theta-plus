"""Phase H — provider-neutral execution adapter + Mock adapter for tests.

Only the operations the verified Dhan v2 workflow actually needs are declared:
submit, status by broker id, status by correlation id (reconciliation after a
timeout), order book and positions. Cancel/modify are intentionally NOT part of
V1's execution path (no pending-order management is performed).

AUTOMATED TESTS MUST USE `MockExecutionAdapter` — it never touches a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from lib.dates import IST
from models.execution_models import BrokerPosition, OrderRequest


class ExecutionError(RuntimeError):
    """Any broker-side execution failure (already redacted)."""


class ExecutionTimeout(ExecutionError):
    """Ambiguous outcome: the request may or may not have reached the broker."""


@dataclass(frozen=True)
class BrokerOrderAck:
    """Broker acknowledgement (Dhan: {orderId, orderStatus})."""

    broker_order_id: str
    broker_status: str


@dataclass(frozen=True)
class BrokerOrderState:
    """Broker-authoritative order state (subset of Dhan GET /v2/orders/{id})."""

    broker_order_id: str
    broker_status: str
    quantity: int = 0
    filled_quantity: int = 0
    remaining_quantity: int = 0
    average_traded_price: Optional[float] = None
    correlation_id: str = ""
    error_code: str = ""
    error_message: str = ""


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Broker execution boundary. Nothing outside the execution layer may hold one."""

    name: str

    @property
    def configured(self) -> bool: ...

    async def submit_order(self, order: OrderRequest) -> BrokerOrderAck: ...

    async def get_order(self, broker_order_id: str) -> BrokerOrderState: ...

    async def get_order_by_correlation(self, client_order_id: str) -> Optional[BrokerOrderState]: ...

    async def get_order_history(self) -> list[BrokerOrderState]: ...

    async def get_positions(self) -> list[BrokerPosition]: ...


@dataclass
class MockExecutionAdapter:
    """Deterministic in-memory broker double. NEVER places a real order."""

    name: str = "mock"
    is_configured: bool = True
    behaviour: str = "filled"  # filled | acknowledged | partial | rejected | timeout
    fill_quantity: Optional[int] = None
    fill_price: float = 100.0
    positions: list[BrokerPosition] = field(default_factory=list)
    submissions: list[OrderRequest] = field(default_factory=list)
    status_calls: list[str] = field(default_factory=list)
    _orders: dict[str, BrokerOrderState] = field(default_factory=dict)
    _by_correlation: dict[str, BrokerOrderState] = field(default_factory=dict)
    _seq: int = 0

    @property
    def configured(self) -> bool:
        return self.is_configured

    @property
    def submit_count(self) -> int:
        return len(self.submissions)

    async def submit_order(self, order: OrderRequest) -> BrokerOrderAck:
        self.submissions.append(order)
        self._seq += 1
        broker_id = f"MOCK-{self._seq:06d}"
        if self.behaviour == "timeout":
            # The request may have reached the broker: record it for reconciliation.
            state = BrokerOrderState(
                broker_order_id=broker_id, broker_status="PENDING",
                quantity=order.quantity, remaining_quantity=order.quantity,
                correlation_id=order.client_order_id,
            )
            self._orders[broker_id] = state
            self._by_correlation[order.client_order_id] = state
            raise ExecutionTimeout("mock broker timeout")
        if self.behaviour == "rejected":
            state = BrokerOrderState(
                broker_order_id=broker_id, broker_status="REJECTED",
                quantity=order.quantity, remaining_quantity=order.quantity,
                correlation_id=order.client_order_id,
                error_code="DH-905", error_message="mock rejection",
            )
        elif self.behaviour == "partial":
            filled = self.fill_quantity if self.fill_quantity is not None else max(order.quantity - 1, 0)
            state = BrokerOrderState(
                broker_order_id=broker_id, broker_status="PART_TRADED",
                quantity=order.quantity, filled_quantity=filled,
                remaining_quantity=order.quantity - filled,
                average_traded_price=self.fill_price,
                correlation_id=order.client_order_id,
            )
        elif self.behaviour == "acknowledged":
            state = BrokerOrderState(
                broker_order_id=broker_id, broker_status="PENDING",
                quantity=order.quantity, remaining_quantity=order.quantity,
                correlation_id=order.client_order_id,
            )
        else:  # filled
            state = BrokerOrderState(
                broker_order_id=broker_id, broker_status="TRADED",
                quantity=order.quantity, filled_quantity=order.quantity,
                remaining_quantity=0, average_traded_price=self.fill_price,
                correlation_id=order.client_order_id,
            )
        self._orders[broker_id] = state
        self._by_correlation[order.client_order_id] = state
        return BrokerOrderAck(broker_order_id=state.broker_order_id, broker_status=state.broker_status)

    async def get_order(self, broker_order_id: str) -> BrokerOrderState:
        self.status_calls.append(broker_order_id)
        state = self._orders.get(broker_order_id)
        if state is None:
            raise ExecutionError(f"unknown broker order {broker_order_id}")
        return state

    async def get_order_by_correlation(self, client_order_id: str) -> Optional[BrokerOrderState]:
        self.status_calls.append(client_order_id)
        return self._by_correlation.get(client_order_id)

    async def get_order_history(self) -> list[BrokerOrderState]:
        return list(self._orders.values())

    async def get_positions(self) -> list[BrokerPosition]:
        return list(self.positions)

    def now(self) -> datetime:
        return datetime.now(IST)
