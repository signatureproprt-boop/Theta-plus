"""Phase B — feature engine: snapshot(+history, +bars) -> MarketFeatures.

Pure and deterministic: same inputs => identical features. Timestamps are
passed explicitly (no wall-clock reads) so replay can reuse this unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from data.validators import (
    aggregate_health,
    data_age_seconds,
    snapshot_health,
    OK,
    STALE,
    INVALID,
    MISSING,
    INCOMPLETE,
)
from engines.atm_engine import calculate_atm, select_strikes
from engines.oi_engine import compute_oi
from engines.pcr_engine import compute_pcr, pcr_change, pcr_trend_from_history
from engines.price_engine import price_features
from engines.vwap_engine import Bar, vwap_features
from models.feature_models import AtmFeatures, MarketFeatures, PcrFeatures
from models.market_models import DataHealth, HealthCode, MarketSnapshot

FEATURE_ENGINE_VERSION = "1.0.0"


def build_features(
    snapshot: MarketSnapshot,
    config,
    bars: Sequence[Bar] = (),
    pcr_history: Sequence[tuple[datetime, float]] = (),
    prev_snapshot: Optional[MarketSnapshot] = None,
) -> MarketFeatures:
    """Build the deterministic feature set for one snapshot.

    `pcr_history` holds PRIOR (timestamp, total_pcr) samples oldest->newest
    (excluding the current one); the engine appends the current PCR before
    classifying the trend. `prev_snapshot` enables OI/PCR change features.
    """
    now = snapshot.timestamp
    spot = snapshot.tick.close
    interval = snapshot.chain.strike_interval or config.strike_interval

    # --- ATM ---------------------------------------------------------------
    atm_strike = calculate_atm(spot, interval)
    strikes = select_strikes(atm_strike, interval, config.atm_range)
    by_strike = {r.strike: r for r in snapshot.chain.rows}
    missing = [s for s in strikes if s not in by_strike]
    atm_row = by_strike.get(atm_strike)
    atm = AtmFeatures(
        atm_strike=atm_strike,
        strike_interval=interval,
        strikes=strikes,
        atm_ce=atm_row.ce if atm_row else None,
        atm_pe=atm_row.pe if atm_row else None,
        valid=atm_row is not None and not missing,
        reason="" if not missing else f"missing strikes in ATM window: {missing}",
    )

    # --- PCR ----------------------------------------------------------------
    pcr = compute_pcr(snapshot.chain, atm_strike, config.atm_range)
    if prev_snapshot is not None:
        prev_atm = calculate_atm(prev_snapshot.tick.close, prev_snapshot.chain.strike_interval or interval)
        prev_pcr = compute_pcr(prev_snapshot.chain, prev_atm, config.atm_range)
        change = pcr_change(pcr.total_pcr, prev_pcr.total_pcr)
    else:
        change = None
    trend = pcr_trend_from_history(
        list(pcr_history) + ([(now, pcr.total_pcr)] if pcr.total_pcr is not None else []),
        config.pcr_lookback,
        config.pcr_flat_band,
    )
    pcr = pcr.model_copy(update={"pcr_change": change, "pcr_trend": trend})

    # --- OI -----------------------------------------------------------------
    if prev_snapshot is not None:
        prev_atm_oi = calculate_atm(prev_snapshot.tick.close, prev_snapshot.chain.strike_interval or interval)
        prev_oi = compute_oi(prev_snapshot.chain)
    else:
        prev_oi = None
    oi = compute_oi(snapshot.chain, prev_oi)

    # --- VWAP / price structure ---------------------------------------------
    vwap = vwap_features(spot, list(bars))
    price = price_features(spot, list(bars), config.price_lookback, config.price_flat_band_pct)

    # --- Data health (snapshot-level + feature-level overlay) ---------------
    health = snapshot_health(snapshot, now, config.max_data_age_seconds)
    checks = dict(health.checks)
    details = list(health.details)
    if not pcr.valid:
        checks["PCR"] = INVALID
        details.append(f"PCR {INVALID}: {pcr.reason}")
    else:
        checks.setdefault("PCR", OK)
    if not vwap.valid:
        checks["VWAP"] = INVALID
        details.append(f"VWAP {INVALID}: {vwap.reason}")
    else:
        checks.setdefault("VWAP", OK)
    if not oi.valid:
        checks["OI"] = MISSING
        details.append(f"OI {MISSING}: {oi.reason}")
    else:
        checks.setdefault("OI", OK)
    if missing:
        checks["OPTION_CHAIN"] = INCOMPLETE
    status = aggregate_health(checks)
    health = DataHealth(
        status=status,
        age_seconds=data_age_seconds(snapshot.tick.timestamp, now),
        checks=checks,
        details=tuple(details),
    )

    return MarketFeatures(
        symbol=snapshot.instrument,
        timestamp=now,
        spot=spot,
        atm=atm,
        pcr=pcr,
        oi=oi,
        vwap=vwap,
        price=price,
        data_health=health,
        feature_engine_version=FEATURE_ENGINE_VERSION,
    )
