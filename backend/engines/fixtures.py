"""Phase C — deterministic scenario fixtures (research + tests + harness).

Every builder is a PURE function of its arguments: fixed numbers, no RNG, no
clock (default `at` is a fixed IST instant inside the signal window). These
same builders power the /api/signal/evaluate research endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Optional

from engines.atm_engine import calculate_atm
from engines.vwap_engine import Bar
from lib.config import AppConfig, config_from_mapping, load_config
from lib.dates import IST
from models.feature_models import (
    AtmFeatures,
    Direction,
    MarketFeatures,
    OiFeatures,
    PcrFeatures,
    PriceFeatures,
    TrendState,
    VwapFeatures,
)
from models.market_models import DataHealth, HealthCode, OptionChain, OptionChainRow, OptionQuote

# Fixed default instant: 2025-01-15 11:45 IST — inside the 11:30–15:15 window.
DEFAULT_AT = datetime(2025, 1, 15, 11, 45, 0, tzinfo=IST)

DEFAULT_SPOT = 25034.7
DEFAULT_VWAP = 24977.4


def default_config(**overrides) -> AppConfig:
    """Validated config from settings.yaml with optional overrides.

    Nested dicts merge: default_config(weights={"vwap": 30}).
    Invalid overrides raise ConfigError (weights must still sum to 100)."""
    base = load_config().model_dump()
    for nested in ("weights", "invalidation"):
        if nested in overrides:
            overrides[nested] = {**base[nested], **overrides[nested]}
    base.update(overrides)
    return config_from_mapping(base)


def _bars(spot: float, step: float, n: int, at: datetime) -> list[Bar]:
    """n one-minute bars ENDING at `at`; closes walk by `step` per bar."""
    bars: list[Bar] = []
    first_close = spot - step * (n - 1)
    for i in range(n):
        close = first_close + step * i
        bars.append(Bar(high=close + 5.0, low=close - 5.0, close=close, volume=10_000.0 + 100.0 * i))
    return bars


def _chain(at: datetime, spot: float, put_oi: int, call_oi: int, put_chg: int, call_chg: int) -> OptionChain:
    interval = 50
    atm = calculate_atm(spot, interval)
    rows = []
    for k in range(-3, 4):
        strike = atm + k * interval
        rows.append(
            OptionChainRow(
                strike=strike,
                ce=OptionQuote(ltp=max(spot - strike, 0.0) + 55.0, oi=call_oi, oi_change=call_chg, volume=10_000, iv=14.0),
                pe=OptionQuote(ltp=max(strike - spot, 0.0) + 55.0, oi=put_oi, oi_change=put_chg, volume=10_000, iv=14.2),
            )
        )
    return OptionChain(symbol="NIFTY", timestamp=at, strike_interval=interval, rows=tuple(rows))


def _history(at: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    """PCR history samples 5 minutes apart, oldest first (excluding current)."""
    return [(at - timedelta(minutes=5 * (len(values) - i)), v) for i, v in enumerate(values)]


def _health(**overrides: str) -> DataHealth:
    checks = {"DATA_AGE": "OK", "OPTION_CHAIN": "OK", "PCR": "OK", "VWAP": "OK", "OI": "OK"}
    checks.update(overrides)
    status = next((c for c in checks.values() if c != "OK"), "OK")
    return DataHealth(status=HealthCode(status), age_seconds=1.0, checks=checks)


def _features(
    at: datetime,
    spot: float,
    chain: OptionChain,
    bars: list[Bar],
    pcr: PcrFeatures,
    oi: OiFeatures,
    vwap: VwapFeatures,
    price: PriceFeatures,
    atm: AtmFeatures,
    health: DataHealth,
) -> MarketFeatures:
    return MarketFeatures(
        symbol="NIFTY",
        timestamp=at,
        spot=spot,
        atm=atm,
        pcr=pcr,
        oi=oi,
        vwap=vwap,
        price=price,
        data_health=health,
        feature_engine_version="1.0.0",
    )


def _atm_features(atm_strike: int, chain: OptionChain, valid: bool = True, reason: str = "") -> AtmFeatures:
    row = next((r for r in chain.rows if r.strike == atm_strike), None)
    return AtmFeatures(
        atm_strike=atm_strike,
        strike_interval=chain.strike_interval,
        strikes=tuple(r.strike for r in chain.rows),
        atm_ce=row.ce if row else None,
        atm_pe=row.pe if row else None,
        valid=valid and row is not None,
        reason=reason,
    )


def strong_ce_features(at: Optional[datetime] = None) -> MarketFeatures:
    """Bullish regime: spot above VWAP, PCR rising (UP), put OI support,
    puts added faster, price UP. Scores CE 100/100, PE 10/100."""
    at = at or DEFAULT_AT
    spot, interval = DEFAULT_SPOT, 50
    chain = _chain(at, spot, put_oi=12_500, call_oi=10_000, put_chg=1_500, call_chg=300)
    atm_strike = calculate_atm(spot, interval)
    pcr = PcrFeatures(
        total_put_oi=87_500, total_call_oi=70_000, total_pcr=1.25,
        atm_put_oi=62_500, atm_call_oi=50_000, atm_pcr=1.25,
        pcr_change=0.05, pcr_trend=TrendState.UP, quality="OK", valid=True,
    )
    oi = OiFeatures(
        call_oi=70_000, put_oi=87_500, call_oi_change=2_100, put_oi_change=10_500,
        call_interpretation="CALL_OI_ADDED: 70,000 (+2,100 vs previous snapshot)",
        put_interpretation="PUT_OI_ADDED: 87,500 (+10,500 vs previous snapshot)",
        valid=True,
    )
    vwap = VwapFeatures(vwap=DEFAULT_VWAP, distance=57.3, distance_percent=0.2293,
                        above_vwap=True, below_vwap=False, valid=True)
    price = PriceFeatures(short_term_direction=Direction.UP, recent_high=spot + 5, recent_low=spot - 7,
                          structure_state="WITHIN_RANGE", available=True,
                          reason="spot 25034.70 vs close 25030.70 5 bars ago (+0.016%)")
    return _features(at, spot, chain, _bars(spot, 0.77, 150, at), pcr, oi, vwap, price,
                     _atm_features(atm_strike, chain), _health())


def strong_pe_features(at: Optional[datetime] = None) -> MarketFeatures:
    """Bearish mirror: spot below VWAP, PCR falling (DOWN), call OI support,
    calls added faster, price DOWN. Scores PE 100/100, CE 10/100."""
    at = at or DEFAULT_AT
    spot, interval = DEFAULT_SPOT, 50
    chain = _chain(at, spot, put_oi=10_000, call_oi=12_500, put_chg=-300, call_chg=1_500)
    atm_strike = calculate_atm(spot, interval)
    pcr = PcrFeatures(
        total_put_oi=70_000, total_call_oi=87_500, total_pcr=0.8,
        atm_put_oi=50_000, atm_call_oi=62_500, atm_pcr=0.8,
        pcr_change=-0.05, pcr_trend=TrendState.DOWN, quality="OK", valid=True,
    )
    oi = OiFeatures(
        call_oi=87_500, put_oi=70_000, call_oi_change=10_500, put_oi_change=-2_100,
        call_interpretation="CALL_OI_ADDED: 87,500 (+10,500 vs previous snapshot)",
        put_interpretation="PUT_OI_UNWOUND: 70,000 (-2,100 vs previous snapshot)",
        valid=True,
    )
    vwap = VwapFeatures(vwap=25077.4, distance=-42.7, distance_percent=-0.1702,
                        above_vwap=False, below_vwap=True, valid=True)
    price = PriceFeatures(short_term_direction=Direction.DOWN, recent_high=spot + 7, recent_low=spot - 5,
                          structure_state="WITHIN_RANGE", available=True,
                          reason="spot 25034.70 vs close 25038.62 5 bars ago (-0.016%)")
    return _features(at, spot, chain, _bars(spot, -0.77, 150, at), pcr, oi, vwap, price,
                     _atm_features(atm_strike, chain), _health())


def weak_features(at: Optional[datetime] = None) -> MarketFeatures:
    """Mixed evidence: CE 45/100, PE 45/100 — both below the 70 minimum."""
    at = at or DEFAULT_AT
    spot, interval = DEFAULT_SPOT, 50
    chain = _chain(at, spot, put_oi=10_000, call_oi=11_000, put_chg=200, call_chg=800)
    atm_strike = calculate_atm(spot, interval)
    pcr = PcrFeatures(
        total_put_oi=70_000, total_call_oi=77_000, total_pcr=0.909,
        atm_put_oi=50_000, atm_call_oi=55_000, atm_pcr=0.909,
        pcr_change=-0.001, pcr_trend=TrendState.FLAT, quality="OK", valid=True,
    )
    oi = OiFeatures(
        call_oi=77_000, put_oi=70_000, call_oi_change=5_600, put_oi_change=1_400,
        call_interpretation="CALL_OI_ADDED: 77,000 (+5,600 vs previous snapshot)",
        put_interpretation="PUT_OI_ADDED: 70,000 (+1,400 vs previous snapshot)",
        valid=True,
    )
    vwap = VwapFeatures(vwap=DEFAULT_VWAP, distance=57.3, distance_percent=0.2293,
                        above_vwap=True, below_vwap=False, valid=True)
    price = PriceFeatures(short_term_direction=Direction.UP, recent_high=spot + 5, recent_low=spot - 7,
                          structure_state="WITHIN_RANGE", available=True,
                          reason="spot 25034.70 vs close 25030.70 5 bars ago (+0.016%)")
    return _features(at, spot, chain, _bars(spot, 0.77, 150, at), pcr, oi, vwap, price,
                     _atm_features(atm_strike, chain), _health())


def conflict_features(at: Optional[datetime] = None) -> MarketFeatures:
    """Symmetric OI (both sides pass OI_SUPPORT/OI_CHANGE), CE-friendly VWAP and
    price. CE 80/100, PE 45/100. With the V1 rule set mutual >=70 qualification
    is logically impossible (PCR/VWAP/price are strict complements), so the
    CONFLICTING_SIGNALS path is exercised via minimum_score=0 in tests — scores
    stay honest (80 and 45), only the threshold drops."""
    at = at or DEFAULT_AT
    spot, interval = DEFAULT_SPOT, 50
    chain = _chain(at, spot, put_oi=11_000, call_oi=11_000, put_chg=0, call_chg=0)
    atm_strike = calculate_atm(spot, interval)
    pcr = PcrFeatures(
        total_put_oi=77_000, total_call_oi=77_000, total_pcr=1.0,
        atm_put_oi=55_000, atm_call_oi=55_000, atm_pcr=1.0,
        pcr_change=0.0, pcr_trend=TrendState.FLAT, quality="OK", valid=True,
    )
    oi = OiFeatures(
        call_oi=77_000, put_oi=77_000, call_oi_change=0, put_oi_change=0,
        call_interpretation="CALL_OI_UNCHANGED: 77,000 (0 vs previous snapshot)",
        put_interpretation="PUT_OI_UNCHANGED: 77,000 (0 vs previous snapshot)",
        valid=True,
    )
    vwap = VwapFeatures(vwap=DEFAULT_VWAP, distance=57.3, distance_percent=0.2293,
                        above_vwap=True, below_vwap=False, valid=True)
    price = PriceFeatures(short_term_direction=Direction.UP, recent_high=spot + 5, recent_low=spot - 7,
                          structure_state="WITHIN_RANGE", available=True,
                          reason="spot 25034.70 vs close 25030.70 5 bars ago (+0.016%)")
    return _features(at, spot, chain, _bars(spot, 0.77, 150, at), pcr, oi, vwap, price,
                     _atm_features(atm_strike, chain), _health())


# --- health-gate variants: strong CE economics, one broken input -------------

def stale_features(at: Optional[datetime] = None) -> MarketFeatures:
    f = strong_ce_features(at)
    return f.model_copy(update={"data_health": _health(DATA_AGE="STALE")})


def invalid_pcr_features(at: Optional[datetime] = None) -> MarketFeatures:
    at = at or DEFAULT_AT
    f = strong_ce_features(at)
    pcr = f.pcr.model_copy(update={
        "total_pcr": None, "atm_pcr": None, "pcr_trend": TrendState.INSUFFICIENT_DATA,
        "valid": False, "quality": "INVALID: total call OI is zero",
        "reason": "PCR undefined: zero total call OI",
    })
    return f.model_copy(update={"pcr": pcr, "data_health": _health(PCR="INVALID")})


def invalid_vwap_features(at: Optional[datetime] = None) -> MarketFeatures:
    at = at or DEFAULT_AT
    f = strong_ce_features(at)
    vwap = f.vwap.model_copy(update={
        "vwap": None, "distance": None, "distance_percent": None,
        "above_vwap": False, "below_vwap": False, "valid": False,
        "reason": "VWAP unavailable: no session bars",
    })
    return f.model_copy(update={"vwap": vwap, "data_health": _health(VWAP="INVALID")})


def chain_incomplete_features(at: Optional[datetime] = None) -> MarketFeatures:
    at = at or DEFAULT_AT
    f = strong_ce_features(at)
    atm = f.atm.model_copy(update={
        "atm_ce": None, "atm_pe": None, "valid": False,
        "reason": "missing strikes in ATM window: [25050]",
    })
    return f.model_copy(update={"atm": atm, "data_health": _health(OPTION_CHAIN="INCOMPLETE")})


# --- invalidation variants (healthy data, opposite VWAP position) ------------

def ce_below_vwap_features(at: Optional[datetime] = None) -> MarketFeatures:
    """Strong-CE economics but spot BELOW VWAP — triggers CE invalidation."""
    at = at or DEFAULT_AT
    f = strong_ce_features(at)
    vwap = f.vwap.model_copy(update={"vwap": 25055.0, "distance": -20.3,
                                     "distance_percent": -0.081, "above_vwap": False, "below_vwap": True})
    return f.model_copy(update={"vwap": vwap})


def pe_above_vwap_features(at: Optional[datetime] = None) -> MarketFeatures:
    """Strong-PE economics but spot ABOVE VWAP — triggers PE invalidation."""
    at = at or DEFAULT_AT
    f = strong_pe_features(at)
    vwap = f.vwap.model_copy(update={"vwap": 25015.0, "distance": 19.7,
                                     "distance_percent": 0.0787, "above_vwap": True, "below_vwap": False})
    return f.model_copy(update={"vwap": vwap})


def ce_pcr_dropped_features(new_pcr: float, at: Optional[datetime] = None) -> MarketFeatures:
    """CE with PCR fallen from the PCR-at-signal (reversal-invalidation input)."""
    at = at or DEFAULT_AT
    f = strong_ce_features(at)
    pcr = f.pcr.model_copy(update={"total_pcr": new_pcr, "pcr_change": round(new_pcr - 1.25, 4)})
    return f.model_copy(update={"pcr": pcr})


SCENARIO_BUILDERS: dict[str, Callable[[Optional[datetime]], MarketFeatures]] = {
    "strong_ce": strong_ce_features,
    "strong_pe": strong_pe_features,
    "weak": weak_features,
    "conflict": conflict_features,
    "stale": stale_features,
    "invalid_pcr": invalid_pcr_features,
    "invalid_vwap": invalid_vwap_features,
    "chain_incomplete": chain_incomplete_features,
    "ce_below_vwap": ce_below_vwap_features,
    "pe_above_vwap": pe_above_vwap_features,
}
