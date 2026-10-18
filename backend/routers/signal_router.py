"""Phase C — read-only research-harness endpoints for the signal engine.

SIGNAL-ONLY: these endpoints evaluate and explain; they never place, modify or
simulate broker orders (no execution code exists anywhere in V1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from engines.fixtures import SCENARIO_BUILDERS
from engines.signal_engine import evaluate, evaluate_and_apply
from engines.signal_memory import SignalMemory
from lib.config import get_config
from lib.dates import IST
from models.signal_models import SignalDecision

router = APIRouter(prefix="/signal", tags=["signal"])

# Process-level in-memory state for use_memory=True. Deliberately not persisted
# (Phase C §23); it resets on process restart. Default requests are pure.
_memory = SignalMemory()


class EvaluateRequest(BaseModel):
    scenario: Literal[
        "strong_ce",
        "strong_pe",
        "weak",
        "conflict",
        "stale",
        "invalid_pcr",
        "invalid_vwap",
        "chain_incomplete",
        "ce_below_vwap",
        "pe_above_vwap",
    ] = "strong_ce"
    at_time: Optional[datetime] = None  # default: now (IST); naive => IST
    use_memory: bool = False            # true: run through the state holder (ramp/cooldown/invalidation)


@router.post("/evaluate", response_model=SignalDecision)
async def evaluate_signal(req: EvaluateRequest) -> SignalDecision:
    config = get_config()
    now = req.at_time or datetime.now(IST)
    features = SCENARIO_BUILDERS[req.scenario](now)
    if req.use_memory:
        return evaluate_and_apply(_memory, features, config, now)
    # Pure: fresh state, no side effects, fully deterministic.
    return evaluate(features, config, SignalMemory().snapshot(), now)


@router.get("/state")
async def signal_state() -> dict:
    snap = _memory.snapshot()
    return {
        "state": snap.state.value,
        "current_side": snap.current_side,
        "signal_timestamp": snap.signal_timestamp,
        "cooldown_until": snap.cooldown_until,
        "signal_pcr": snap.signal_pcr,
        "last_decision": snap.last_decision,
    }


@router.post("/reset")
async def reset_signal_state() -> dict:
    _memory.reset()
    return {"reset": True}
