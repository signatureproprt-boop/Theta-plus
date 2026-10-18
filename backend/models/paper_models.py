"""Phase G — PAPER trading models.

PAPER / HYPOTHETICAL ONLY. A PaperPosition is a research record: there is no
broker position, no order id, no quantity from real capital and no execution
field of any kind. P&L is a HYPOTHETICAL computation from deterministic backend
option LTPs plus explicitly assumed slippage/costs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

# Position states (§12) — deliberately no broker state exists.
PAPER_OPEN = "OPEN"
PAPER_TARGET = "TARGET"
PAPER_STOPLOSS = "STOPLOSS"
PAPER_INVALIDATED = "INVALIDATED"
PAPER_EXPIRED = "EXPIRED"
PAPER_NO_DATA = "NO_DATA"
PAPER_AMBIGUOUS = "AMBIGUOUS"  # same-observation target+stop (Phase E semantics)

CLOSED_STATES = (PAPER_TARGET, PAPER_STOPLOSS, PAPER_INVALIDATED, PAPER_EXPIRED, PAPER_AMBIGUOUS)

PAPER_LABEL = "PAPER / HYPOTHETICAL — not broker execution"


class PaperPosition(BaseModel):
    """One paper position (§11). Immutable; transitions create a new copy."""

    model_config = ConfigDict(frozen=True)

    paper_position_id: str
    signal_id: str
    timestamp: datetime
    underlying: str = "NIFTY"
    option_type: str = "CE"  # CE | PE
    strike: Optional[int] = None

    entry_price: Optional[float] = None
    current_price: Optional[float] = None
    target_price: Optional[float] = None
    stoploss_price: Optional[float] = None
    quantity: int = 1

    state: str = PAPER_OPEN
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: str = ""

    gross_points: Optional[float] = None
    gross_pnl: Optional[float] = None
    assumed_slippage_points: float = 0.0
    costs: float = 0.0
    net_pnl: Optional[float] = None

    data_origin: str = "LIVE"
    data_origin_label: str = ""
    mode: str = "PAPER"
    label: str = PAPER_LABEL

    # Audit links back to the frozen strategy that produced the signal.
    strategy_version: str = ""
    feature_version: str = ""

    @property
    def is_open(self) -> bool:
        return self.state == PAPER_OPEN


class PaperSummary(BaseModel):
    """Aggregate paper stats (§22). Always labeled PAPER / HYPOTHETICAL."""

    model_config = ConfigDict(frozen=True)

    mode: str = "RESEARCH"
    paper_trading_enabled: bool = False
    label: str = PAPER_LABEL
    data_origin: str = "LIVE"
    data_origin_label: str = ""

    total_positions: int = 0
    open_positions: int = 0
    target: int = 0
    stoploss: int = 0
    invalidated: int = 0
    expired: int = 0
    ambiguous: int = 0
    no_data: int = 0

    gross_points: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0

    quantity: int = 1
    target_points: int = 35
    stoploss_points: int = 30
    max_open_positions: int = 1
    cost_note: str = ""
    open_position: Optional[PaperPosition] = None
