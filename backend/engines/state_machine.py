"""Phase C — state machine: explicit states and validated transitions.

States: WAIT -> WATCH -> ENTRY_CANDIDATE -> ACTIVE, with exit states
INVALIDATED / TARGET / STOPLOSS / EXPIRED.

In Phase C (signal-only) ACTIVE is a RESEARCH/SIGNAL state only — it never
means a broker position exists. There is no broker position anywhere.
"""

from __future__ import annotations

from enum import Enum


class SignalState(str, Enum):
    WAIT = "WAIT"
    WATCH = "WATCH"
    ENTRY_CANDIDATE = "ENTRY_CANDIDATE"
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"
    TARGET = "TARGET"
    STOPLOSS = "STOPLOSS"
    EXPIRED = "EXPIRED"


class IllegalTransitionError(ValueError):
    """Raised when a transition is not part of the state graph."""


VALID_TRANSITIONS: dict[SignalState, frozenset[SignalState]] = {
    SignalState.WAIT: frozenset({SignalState.WATCH}),
    SignalState.WATCH: frozenset({SignalState.ENTRY_CANDIDATE, SignalState.WAIT}),
    SignalState.ENTRY_CANDIDATE: frozenset({SignalState.ACTIVE, SignalState.WAIT}),
    SignalState.ACTIVE: frozenset(
        {SignalState.INVALIDATED, SignalState.TARGET, SignalState.STOPLOSS, SignalState.EXPIRED}
    ),
    SignalState.INVALIDATED: frozenset({SignalState.WAIT}),
    SignalState.TARGET: frozenset({SignalState.WAIT}),
    SignalState.STOPLOSS: frozenset({SignalState.WAIT}),
    SignalState.EXPIRED: frozenset({SignalState.WAIT}),
}


def transition(current: SignalState, target: SignalState) -> SignalState:
    """Validate and return `target`; raise IllegalTransitionError if illegal.

    Staying in the same state is always allowed and simply returns `current`.
    """
    if current == target:
        return current
    if target not in VALID_TRANSITIONS[current]:
        legal = ", ".join(sorted(s.value for s in VALID_TRANSITIONS[current]))
        raise IllegalTransitionError(
            f"illegal state transition {current.value} -> {target.value} (legal: {legal})"
        )
    return target
