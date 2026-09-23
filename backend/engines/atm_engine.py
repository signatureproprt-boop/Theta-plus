"""Phase B — ATM engine: strike arithmetic only, fully deterministic."""

from __future__ import annotations

import math


def calculate_atm(spot: float, strike_interval: int) -> int:
    """Round spot to the nearest strike (half-up) for the given interval.

    Spot 25034 / interval 50 -> 25050. Exact strikes are unchanged. Half-up is
    used instead of banker's rounding so behavior is unambiguous at midpoints.
    """
    if strike_interval <= 0:
        raise ValueError(f"strike_interval must be positive (got {strike_interval})")
    return int(math.floor(spot / strike_interval + 0.5)) * strike_interval


def select_strikes(atm: int, strike_interval: int, atm_range: int) -> tuple[int, ...]:
    """The 2*atm_range+1 strikes centered on ATM, ascending."""
    if atm_range < 0:
        raise ValueError(f"atm_range must be >= 0 (got {atm_range})")
    return tuple(atm + k * strike_interval for k in range(-atm_range, atm_range + 1))
