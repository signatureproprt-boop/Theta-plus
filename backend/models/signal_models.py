"""Phase C — signal models: RuleResult, SignalDecision, decision constants.

The score fields measure CONFIGURED RULE CONFIRMATION only. They are NOT a
probability, accuracy, or chance of profit — never display "82%"; display
"82/100". The model deliberately has no probability-like field.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

DECISION_CE_SETUP = "CE_SETUP"
DECISION_PE_SETUP = "PE_SETUP"
DECISION_WAIT = "WAIT"
DECISION_CE_INVALIDATED = "CE_INVALIDATED"
DECISION_PE_INVALIDATED = "PE_INVALIDATED"

SIDE_CE = "CE"
SIDE_PE = "PE"


class RuleResult(BaseModel):
    """One evaluated condition. Every score must be explainable."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    side: str
    passed: bool
    available: bool
    score_awarded: int
    max_score: int
    observed_value: str
    reason: str


class SignalDecision(BaseModel):
    """Immutable, auditable signal decision (master prompt §14/§41).

    Carries the full rule-level evidence so every decision can be reconstructed
    later. No execution fields exist — this is a signal, not an order.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    symbol: str

    decision: str  # one of the DECISION_* constants
    state: str     # SignalState value (post-apply when produced via evaluate_and_apply)

    ce_score: int
    pe_score: int
    ce_max_score: int
    pe_max_score: int
    qualifying_side: Optional[str] = None  # set when exactly one side reached the minimum

    reasons: tuple[str, ...] = ()
    ce_rules: tuple[RuleResult, ...] = ()
    pe_rules: tuple[RuleResult, ...] = ()

    atm: Optional[int] = None
    spot: Optional[float] = None
    vwap: Optional[float] = None
    pcr: Optional[float] = None
    pcr_trend: Optional[str] = None

    strategy_version: str
    feature_engine_version: str
    config_version: str

    data_health: str
    cooldown_until: Optional[datetime] = None
