"""Phase B — OI engine: raw call/put OI levels, changes vs previous snapshot,
and a deterministic interpretation layer.

Raw values are always preserved — never hidden behind a single bullish/bearish
label, and no interpretation here claims to predict price movement.
"""

from __future__ import annotations

from typing import Optional

from models.feature_models import OiFeatures
from models.market_models import OptionChain


def _interpretation(side: str, level: int, change: Optional[int]) -> str:
    if change is None:
        return f"{side}_OI_BASELINE: {level:,} (no previous snapshot)"
    if change > 0:
        return f"{side}_OI_ADDED: {level:,} ({change:+,} vs previous snapshot)"
    if change < 0:
        return f"{side}_OI_UNWOUND: {level:,} ({change:+,} vs previous snapshot)"
    return f"{side}_OI_UNCHANGED: {level:,} (0 vs previous snapshot)"


def compute_oi(chain: OptionChain, previous: Optional[OiFeatures] = None) -> OiFeatures:
    """OI levels + changes vs `previous`. Changes stay None (never guessed)
    when no previous snapshot exists."""
    call_oi = sum(r.ce.oi for r in chain.rows)
    put_oi = sum(r.pe.oi for r in chain.rows)

    call_change: Optional[int] = None
    put_change: Optional[int] = None
    if previous is not None:
        call_change = call_oi - previous.call_oi
        put_change = put_oi - previous.put_oi

    if not chain.rows:
        return OiFeatures(
            call_oi=0,
            put_oi=0,
            call_oi_change=call_change,
            put_oi_change=put_change,
            call_interpretation=_interpretation("CALL", 0, call_change),
            put_interpretation=_interpretation("PUT", 0, put_change),
            valid=False,
            reason="empty option chain; OI unavailable",
        )

    return OiFeatures(
        call_oi=call_oi,
        put_oi=put_oi,
        call_oi_change=call_change,
        put_oi_change=put_change,
        call_interpretation=_interpretation("CALL", call_oi, call_change),
        put_interpretation=_interpretation("PUT", put_oi, put_change),
        valid=True,
        reason="" if previous is not None else "no previous snapshot: OI change unavailable",
    )


def price_oi_relationship(price_direction: str, oi_change: Optional[int], side: str) -> str:
    """Deterministic price/OI relationship label (documented, NOT predictive).

    underlying: PRICE_UP + OI added -> LONG_BUILDUP; PRICE_UP + OI unwound -> SHORT_COVER;
    PRICE_DOWN + OI added -> SHORT_BUILDUP; PRICE_DOWN + OI unwound -> LONG_UNWINDING.
    Option sides use the same four labels with an explicit option prefix.
    """
    if oi_change is None:
        return f"{side}_RELATIONSHIP_UNKNOWN: no OI change data"
    added = oi_change > 0
    up = price_direction == "UP"
    if up and added:
        return f"{side}_LONG_BUILDUP"
    if up and not added:
        return f"{side}_SHORT_COVER"
    if not up and added:
        return f"{side}_SHORT_BUILDUP"
    return f"{side}_LONG_UNWINDING"
