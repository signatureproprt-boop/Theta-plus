"""Phase B — price structure engine: deterministic confirmation features from
what already exists (bars + spot). No new indicators in V1 (no RSI/MACD/...)."""

from __future__ import annotations

from typing import Optional, Sequence

from models.feature_models import Direction, PriceFeatures
from engines.vwap_engine import Bar


def price_features(
    spot: float,
    bars: Sequence[Bar],
    lookback: int = 5,
    flat_band_pct: float = 0.01,
) -> PriceFeatures:
    """Short-term direction, recent high/low and structure state.

    - short_term_direction: spot vs the close `lookback` bars ago (or the first
      close when fewer bars exist), with a %-flat band.
      UP when pct > band, DOWN when pct < -band, FLAT otherwise.
    - recent_high / recent_low over the same window.
    - structure_state: BREAKOUT (spot > recent high), BREAKDOWN (spot < recent
      low) or WITHIN_RANGE.

    Needs at least 2 bars; fewer reports available=False — the rule layer then
    records PRICE_CONFIRMATION unavailable instead of fabricating confirmation.
    """
    if len(bars) < 2:
        return PriceFeatures(
            available=False,
            reason=f"insufficient bars for price structure ({len(bars)} of 2 required)",
        )
    window = list(bars)[-lookback:] if len(bars) >= lookback else list(bars)
    ref_close = window[0].close
    pct = (spot / ref_close - 1.0) * 100.0 if ref_close != 0 else 0.0
    if pct > flat_band_pct:
        direction = Direction.UP
    elif pct < -flat_band_pct:
        direction = Direction.DOWN
    else:
        direction = Direction.FLAT
    recent_high = max(b.high for b in window)
    recent_low = min(b.low for b in window)
    if spot > recent_high:
        structure = "BREAKOUT"
    elif spot < recent_low:
        structure = "BREAKDOWN"
    else:
        structure = "WITHIN_RANGE"
    return PriceFeatures(
        short_term_direction=direction,
        recent_high=recent_high,
        recent_low=recent_low,
        structure_state=structure,
        available=True,
        reason=f"spot {spot:.2f} vs close {ref_close:.2f} {lookback} bars ago ({pct:+.3f}%)",
    )
