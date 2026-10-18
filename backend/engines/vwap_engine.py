"""Phase B — VWAP engine: one deterministic session VWAP formula shared by
live evaluation and (future) replay/backtest. Never two formulas."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from models.feature_models import VwapFeatures


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar (e.g. a 1-minute candle) of the underlying."""

    high: float
    low: float
    close: float
    volume: float
    source: str = "ohlcv"


def typical_price(bar: Bar) -> float:
    return (bar.high + bar.low + bar.close) / 3.0


def compute_vwap(bars: Sequence[Bar]) -> Optional[float]:
    """Session VWAP = sum(typical * volume) / sum(volume).

    Missing, invalid or zero-volume data is unavailable; never substitute a mean.
    """
    if not bars:
        return None
    if any(not all(math.isfinite(v) for v in (b.high, b.low, b.close, b.volume))
           or b.volume < 0 or b.low <= 0 or b.high < b.low or not b.low <= b.close <= b.high
           for b in bars):
        return None
    total_volume = sum(b.volume for b in bars)
    if total_volume <= 0:
        return None
    total_pv = sum(typical_price(b) * b.volume for b in bars)
    return round(total_pv / total_volume, 4)


def vwap_features(spot: float, bars: Sequence[Bar]) -> VwapFeatures:
    """VwapFeatures for the current spot: distance, distance_percent, flags.

    At exactly spot == VWAP neither above_vwap nor below_vwap is set —
    neither direction is confirmed (documented).
    """
    vwap = compute_vwap(bars)
    if vwap is None:
        return VwapFeatures(valid=False, reason="VWAP unavailable: missing/invalid bars or no traded volume")
    distance = round(spot - vwap, 4)
    distance_percent = round(distance / vwap * 100.0, 4) if vwap != 0 else None
    return VwapFeatures(
        vwap=vwap,
        source=bars[-1].source,
        reference_price=spot,
        distance=distance,
        distance_percent=distance_percent,
        above_vwap=spot > vwap,
        below_vwap=spot < vwap,
        valid=True,
        reason="",
    )
