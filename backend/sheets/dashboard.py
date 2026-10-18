"""Phase D — the single dashboard mapping: engine outputs -> control room.

One `build_dashboard(...)` feeds BOTH surfaces that must never diverge:
  * GET /api/dashboard  (web control room)
  * the DASHBOARD tab mirror in Google Sheets
Pure display logic: no strategy rules here, no Google API calls here.

Display law (§4/§5/§17/§18): decisions render as exactly "CE SETUP"/"PE SETUP"/
"WAIT"; scores as "X/100" (never %); health statuses are exactly
OK/WARNING/STALE/ERROR/DISABLED; a stale feed always surfaces as stale and the
signal never hides a safety failure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from engines.signal_memory import SignalStateSnapshot
from models.dashboard_models import (
    DashboardPayload,
    DataHealthPanel,
    HealthItem,
    MarketPanel,
    OiPanel,
    OptionPanel,
    PcrPanel,
    SettingsPanel,
    SheetsStatusPanel,
    SignalPanel,
    SystemInfo,
    SystemLogEntry,
)
from models.feature_models import MarketFeatures
from models.signal_models import (
    DECISION_CE_SETUP,
    DECISION_PE_SETUP,
    DECISION_WAIT,
    SignalDecision,
)

DECISION_LABELS = {
    "CE_SETUP": "CE SETUP",
    "PE_SETUP": "PE SETUP",
    "WAIT": "WAIT",
    "CE_INVALIDATED": "CE INVALIDATED",
    "PE_INVALIDATED": "PE INVALIDATED",
}

# Human-readable rule names for the reason matrix (§6), per side.
RULE_LABELS = {
    "CE": {
        "PCR_CONFIRMATION": "PCR confirmation",
        "VWAP_CONFIRMATION": "Spot above VWAP",
        "OI_SUPPORT": "Put OI support",
        "OI_CHANGE_SUPPORT": "Put OI change support",
        "ATM_DATA_VALID": "ATM/option data valid",
        "PRICE_CONFIRMATION": "Price confirmation",
    },
    "PE": {
        "PCR_CONFIRMATION": "PCR confirmation",
        "VWAP_CONFIRMATION": "Spot below VWAP",
        "OI_SUPPORT": "Call OI support",
        "OI_CHANGE_SUPPORT": "Call OI change support",
        "ATM_DATA_VALID": "ATM/option data valid",
        "PRICE_CONFIRMATION": "Price confirmation",
    },
}

STATUS_OK, STATUS_WARNING, STATUS_STALE, STATUS_ERROR, STATUS_DISABLED = (
    "OK", "WARNING", "STALE", "ERROR", "DISABLED",
)

_CHECK_TO_STATUS = {
    "OK": STATUS_OK,
    "STALE": STATUS_STALE,
    "INVALID": STATUS_ERROR,
    "MISSING": STATUS_WARNING,
    "INCOMPLETE": STATUS_WARNING,
    "DISABLED": STATUS_DISABLED,
}


def health_item(component: str, check_status: str, detail: str = "") -> HealthItem:
    """Map an internal check code to a dashboard status (§4 status values)."""
    return HealthItem(
        component=component,
        status=_CHECK_TO_STATUS.get(check_status, STATUS_ERROR),
        detail=detail,
    )


def overall_status(features: Optional[MarketFeatures], system_enabled: bool, feed_live: bool) -> str:
    """Worst-status aggregate; DISABLED wins when the kill switch is off."""
    if not system_enabled:
        return STATUS_DISABLED
    if features is None:
        return feed_live and STATUS_WARNING or STATUS_DISABLED
    worst = STATUS_OK
    rank = {STATUS_OK: 0, STATUS_WARNING: 1, STATUS_STALE: 2, STATUS_ERROR: 3}
    for item in features.data_health.checks.values():
        status = _CHECK_TO_STATUS.get(item, STATUS_ERROR)
        if rank[status] > rank[worst]:
            worst = status
    return worst


def _side_reasons(decision: SignalDecision, side: str) -> tuple[list[str], list[str]]:
    """(confirmed, unavailable) human labels for the decision's leading side."""
    rules = decision.ce_rules if side == "CE" else decision.pe_rules
    labels = RULE_LABELS[side]
    confirmed = [labels[r.rule_id] for r in rules if r.available and r.passed]
    unavailable = [labels[r.rule_id] for r in rules if not r.available]
    return confirmed, unavailable


def build_dashboard(
    *,
    features: Optional[MarketFeatures],
    decision: Optional[SignalDecision],
    config,
    data_source: str,
    memory: SignalStateSnapshot,
    sheets_status: SheetsStatusPanel,
    log_entries: Sequence[SystemLogEntry],
    generated_at: Optional[datetime] = None,
    atm_pcr_change: Optional[float] = None,
    atm_pcr_trend: str = "INSUFFICIENT_DATA",
) -> DashboardPayload:
    generated_at = generated_at or datetime.now()

    system = SystemInfo(
        enabled=config.enabled,
        strategy_version=config.strategy_version,
        config_version=config.config_version,
        feature_engine_version=config.feature_engine_version,
        data_source=data_source,
    )

    if features is not None:
        market = MarketPanel(
            index=config.instrument,
            spot=features.spot,
            vwap=features.vwap.vwap,
            vwap_distance=features.vwap.distance,
            vwap_distance_percent=features.vwap.distance_percent,
            above_vwap=features.vwap.above_vwap,
            below_vwap=features.vwap.below_vwap,
            atm=features.atm.atm_strike,
            last_update=features.timestamp,
        )
        pcr = PcrPanel(
            total_pcr=features.pcr.total_pcr,
            atm_pcr=features.pcr.atm_pcr,
            pcr_change=features.pcr.pcr_change,
            atm_pcr_change=atm_pcr_change,
            pcr_trend=features.pcr.pcr_trend.value,
            atm_pcr_trend=atm_pcr_trend,
        )
        oi = OiPanel(
            put_oi=features.oi.put_oi,
            call_oi=features.oi.call_oi,
            put_oi_change=features.oi.put_oi_change,
            call_oi_change=features.oi.call_oi_change,
        )
        atm_ce, atm_pe = features.atm.atm_ce, features.atm.atm_pe
        option = OptionPanel(
            atm_strike=features.atm.atm_strike,
            atm_ce_ltp=atm_ce.ltp if atm_ce else None,
            atm_pe_ltp=atm_pe.ltp if atm_pe else None,
            ce_oi=atm_ce.oi if atm_ce else None,
            pe_oi=atm_pe.oi if atm_pe else None,
            ce_oi_change=atm_ce.oi_change if atm_ce else None,
            pe_oi_change=atm_pe.oi_change if atm_pe else None,
            ce_iv=atm_ce.iv if atm_ce else None,
            pe_iv=atm_pe.iv if atm_pe else None,
        )
        items = (
            health_item("DHAN API", "OK" if data_source == "dhan" else "DISABLED",
                        "" if data_source == "dhan" else "simulated feed active"),
            health_item("WEBSOCKET", "OK" if data_source == "dhan" else "DISABLED",
                        "" if data_source == "dhan" else "not used by sim feed"),
            health_item("OPTION CHAIN", features.data_health.checks.get("OPTION_CHAIN", "MISSING")),
            health_item("VWAP", features.data_health.checks.get("VWAP", "MISSING")),
            health_item("DATA AGE", features.data_health.checks.get("DATA_AGE", "MISSING"),
                        f"{features.data_health.age_seconds:.0f} sec" if features.data_health.age_seconds is not None else ""),
        )
        data_health = DataHealthPanel(
            overall=overall_status(features, config.enabled, feed_live=data_source == "dhan"),
            data_age_seconds=features.data_health.age_seconds,
            items=items,
        )
    else:
        market, pcr, oi, option = MarketPanel(index=config.instrument), PcrPanel(), OiPanel(), OptionPanel()
        data_health = DataHealthPanel(overall=overall_status(None, config.enabled, data_source == "dhan"))

    if decision is not None:
        leading = "CE" if decision.ce_score >= decision.pe_score else "PE"
        confirmed, unavailable = _side_reasons(decision, leading)
        signal = SignalPanel(
            decision=DECISION_LABELS.get(decision.decision, decision.decision),
            decision_code=decision.decision,
            ce_score=decision.ce_score,
            pe_score=decision.pe_score,
            ce_max_score=decision.ce_max_score,
            pe_max_score=decision.pe_max_score,
            state=decision.state,
            signal_time=memory.signal_timestamp,
            cooldown_until=memory.cooldown_until,
            reasons=decision.reasons,
            confirmed=tuple(confirmed),
            unavailable=tuple(unavailable),
        )
    else:
        signal = SignalPanel(decision=DECISION_LABELS[DECISION_WAIT], decision_code=DECISION_WAIT)

    settings = SettingsPanel(
        index=config.instrument,
        market_open=config.market_open,
        signal_start=config.signal_start_time,
        signal_end=config.signal_end_time,
        atm_range=config.atm_range,
        pcr_lookback=config.pcr_lookback,
        min_score=config.minimum_score,
        target_points=config.target_points,
        stoploss_points=config.stoploss_points,
        signal_cooldown=config.signal_cooldown_minutes,
        system_enabled=config.enabled,
        timezone=config.timezone,
        strategy_version=config.strategy_version,
        feature_engine_version=config.feature_engine_version,
    )

    return DashboardPayload(
        generated_at=generated_at,
        system=system,
        market=market,
        pcr=pcr,
        oi=oi,
        signal=signal,
        option=option,
        data_health=data_health,
        settings=settings,
        sheets=sheets_status,
        system_log=tuple(log_entries),
    )


def dashboard_grid(payload: DashboardPayload) -> list[list]:
    """The DASHBOARD tab mirror: KEY/VALUE rows in control-room order (§4)."""
    m, p, o, s, d = payload.market, payload.pcr, payload.oi, payload.signal, payload.data_health
    fmt = lambda v, n=2: (round(v, n) if isinstance(v, (int, float)) else (v or ""))  # noqa: E731
    return [
        ["PCR TRADING SYSTEM", ""],
        ["SYSTEM", "ENABLED" if payload.system.enabled else "DISABLED"],
        ["STRATEGY_VERSION", payload.system.strategy_version],
        ["INDEX", m.index],
        ["SPOT", fmt(m.spot)],
        ["VWAP", fmt(m.vwap)],
        ["VWAP DISTANCE", fmt(m.vwap_distance)],
        ["ATM", m.atm if m.atm is not None else ""],
        ["LAST UPDATE", m.last_update.isoformat() if m.last_update else ""],
        ["TOTAL PCR", fmt(p.total_pcr, 4)],
        ["ATM PCR", fmt(p.atm_pcr, 4)],
        ["PCR CHANGE", fmt(p.pcr_change, 4)],
        ["PCR TREND", p.pcr_trend],
        ["ATM PCR TREND", p.atm_pcr_trend],
        ["PUT OI", o.put_oi if o.put_oi is not None else ""],
        ["CALL OI", o.call_oi if o.call_oi is not None else ""],
        ["PUT OI CHANGE", o.put_oi_change if o.put_oi_change is not None else ""],
        ["CALL OI CHANGE", o.call_oi_change if o.call_oi_change is not None else ""],
        ["CE SCORE", s.ce_score],
        ["PE SCORE", s.pe_score],
        ["MAX CE SCORE", s.ce_max_score],
        ["MAX PE SCORE", s.pe_max_score],
        ["FINAL SIGNAL", s.decision],
        ["CURRENT STATE", s.state],
        ["SIGNAL TIME", s.signal_time.isoformat() if s.signal_time else ""],
        ["COOLDOWN UNTIL", s.cooldown_until.isoformat() if s.cooldown_until else ""],
        ["ATM CE LTP", fmt(payload.option.atm_ce_ltp)],
        ["ATM PE LTP", fmt(payload.option.atm_pe_ltp)],
        ["DHAN API", next((i.status for i in d.items if i.component == "DHAN API"), "")],
        ["WEBSOCKET", next((i.status for i in d.items if i.component == "WEBSOCKET"), "")],
        ["OPTION CHAIN", next((i.status for i in d.items if i.component == "OPTION CHAIN"), "")],
        ["VWAP", next((i.status for i in d.items if i.component == "VWAP"), "")],
        ["DATA AGE", f"{d.data_age_seconds:.0f} sec" if d.data_age_seconds is not None else ""],
        ["OVERALL STATUS", d.overall],
        ["REASON", " | ".join(s.reasons)[:900]],
    ]
