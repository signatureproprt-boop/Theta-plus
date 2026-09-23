"""Phase C — in-memory signal state holder (master prompt §23).

Deliberately NOT persisted: persistence belongs to later phases. Holds exactly
the runtime state the cooldown/state machine need:
  current_state, current_side, signal_timestamp, last_decision, cooldown_until
plus signal_pcr (the PCR at emission, used by the deterministic PCR-reversal
invalidation rule).

`evaluate()` stays pure; ALL mutation happens here in `apply()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from engines.state_machine import SignalState, transition
from lib.dates import to_ist
from models.signal_models import (
    DECISION_CE_INVALIDATED,
    DECISION_CE_SETUP,
    DECISION_PE_INVALIDATED,
    DECISION_PE_SETUP,
    DECISION_WAIT,
)


@dataclass(frozen=True)
class SignalStateSnapshot:
    """Read-only view handed INTO the pure evaluator."""

    state: SignalState = SignalState.WAIT
    current_side: Optional[str] = None
    signal_timestamp: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    signal_pcr: Optional[float] = None
    last_decision: Optional[str] = None


class SignalMemory:
    """Runtime signal state. Mutates only through apply()."""

    def __init__(self) -> None:
        self._state: SignalState = SignalState.WAIT
        self._side: Optional[str] = None
        self._signal_ts: Optional[datetime] = None
        self._cooldown_until: Optional[datetime] = None
        self._signal_pcr: Optional[float] = None
        self._last_decision: Optional[str] = None

    def snapshot(self) -> SignalStateSnapshot:
        return SignalStateSnapshot(
            state=self._state,
            current_side=self._side,
            signal_timestamp=self._signal_ts,
            cooldown_until=self._cooldown_until,
            signal_pcr=self._signal_pcr,
            last_decision=self._last_decision,
        )

    def reset(self) -> None:
        self.__init__()  # noqa: PLC2801 — deliberate full reset

    def apply(self, decision: SignalDecision, now: datetime, cooldown_minutes: int) -> SignalState:
        """Apply a surfaced decision; every change goes through transition().

        Documented Phase C semantics:
        * CE/PE SETUP (a fresh emission) advances the research ramp one step:
          WAIT -> WATCH -> ENTRY_CANDIDATE -> ACTIVE, and (re)starts cooldown.
        * WAIT during cooldown does NOT advance the ramp (cooldown suppresses
          emissions, and the ramp is driven by emissions only).
        * Invalidation while ACTIVE -> INVALIDATED; the next evaluation
          recovers INVALIDATED (and TARGET/STOPLOSS/EXPIRED) -> WAIT.
        * ACTIVE + TIME_FILTER_AFTER_END -> EXPIRED (session over).
        """
        now = to_ist(now)
        d = decision.decision

        # Recovery from exit states back to WAIT (before anything else).
        if d == DECISION_WAIT and self._state in (
            SignalState.INVALIDATED,
            SignalState.TARGET,
            SignalState.STOPLOSS,
            SignalState.EXPIRED,
        ):
            self._state = transition(self._state, SignalState.WAIT)
            self._side = None
            self._signal_ts = None
            self._cooldown_until = None
            self._signal_pcr = None

        if d in (DECISION_CE_INVALIDATED, DECISION_PE_INVALIDATED):
            if self._state == SignalState.ACTIVE:
                self._state = transition(self._state, SignalState.INVALIDATED)
            self._last_decision = d
            return self._state

        if d in (DECISION_CE_SETUP, DECISION_PE_SETUP):
            side = "CE" if d == DECISION_CE_SETUP else "PE"
            ramp = {
                SignalState.WAIT: SignalState.WATCH,
                SignalState.WATCH: SignalState.ENTRY_CANDIDATE,
                SignalState.ENTRY_CANDIDATE: SignalState.ACTIVE,
            }
            if self._state in ramp:
                self._state = transition(self._state, ramp[self._state])
            self._side = side
            self._signal_ts = now
            self._cooldown_until = now + timedelta(minutes=cooldown_minutes)
            self._signal_pcr = decision.pcr
            self._last_decision = d
            return self._state

        # WAIT: expire an ACTIVE research signal after the session end.
        if (
            self._state == SignalState.ACTIVE
            and "TIME_FILTER_AFTER_END" in decision.reasons
        ):
            self._state = transition(self._state, SignalState.EXPIRED)
        self._last_decision = d
        return self._state
