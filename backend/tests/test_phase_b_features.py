"""Phase B regression — feature engines: ATM, PCR, OI, VWAP, price (18 tests)."""

from datetime import datetime, timedelta

from engines.atm_engine import calculate_atm, select_strikes
from engines.feature_engine import FEATURE_ENGINE_VERSION, build_features
from engines.oi_engine import compute_oi
from engines.pcr_engine import compute_pcr, pcr_change, pcr_trend_from_history
from engines.price_engine import price_features
from engines.vwap_engine import Bar, compute_vwap, vwap_features
from data.sim_feed import build_snapshot, day_bars
from lib.config import load_config
from lib.dates import IST
from models.feature_models import TrendState
from models.market_models import InstrumentTick, MarketSnapshot, OptionChain, OptionChainRow, OptionQuote

NOW = datetime(2025, 1, 15, 11, 45, 0, tzinfo=IST)
CFG = load_config()


def _row(strike: int, ce_oi: int, pe_oi: int) -> OptionChainRow:
    return OptionChainRow(strike=strike, ce=OptionQuote(ltp=100.0, oi=ce_oi), pe=OptionQuote(ltp=100.0, oi=pe_oi))


def _chain(rows: list[tuple[int, int, int]]) -> OptionChain:
    return OptionChain(timestamp=NOW, strike_interval=50, rows=tuple(_row(*r) for r in rows))


# --- ATM ---------------------------------------------------------------------


def test_atm_rounds_to_strike_interval():
    assert calculate_atm(25034, 50) == 25050


def test_atm_half_up_at_midpoint():
    assert calculate_atm(25025, 50) == 25050  # documented half-up, never banker's


def test_atm_exact_strike_unchanged():
    assert calculate_atm(25050, 50) == 25050


def test_atm_custom_interval_and_strike_selection():
    assert calculate_atm(25099, 100) == 25100  # interval is never assumed to be 50
    assert select_strikes(25050, 50, 3) == (24900, 24950, 25000, 25050, 25100, 25150, 25200)


# --- PCR ---------------------------------------------------------------------


def test_pcr_total():
    pcr = compute_pcr(_chain([(s, 1000, 1200) for s in range(24900, 25201, 50)]), 25050, 3)
    assert pcr.total_call_oi == 7000 and pcr.total_put_oi == 8400
    assert pcr.total_pcr == 1.2 and pcr.valid


def test_pcr_atm_range_only():
    rows = [(25000, 1000, 1000), (25050, 400, 500), (25100, 400, 500), (25150, 400, 500),
            (25200, 1000, 1000), (25000 - 50, 900, 900), (25200 + 50, 900, 900)]
    pcr = compute_pcr(_chain(rows), 25100, 1)
    assert pcr.atm_call_oi == 1200 and pcr.atm_put_oi == 1500
    assert pcr.atm_pcr == 1.25
    assert pcr.total_pcr != pcr.atm_pcr  # total and ATM-range are separate measures


def test_pcr_zero_call_oi_invalid_not_crash():
    pcr = compute_pcr(_chain([(25050, 0, 0)]), 25050, 3)
    assert pcr.total_pcr is None and not pcr.valid
    assert pcr.quality.startswith("INVALID")


def test_pcr_missing_strikes_incomplete():
    rows = [(s, 1000, 1200) for s in (24900, 24950, 25000, 25050, 25100)]  # 25150/25200 missing
    pcr = compute_pcr(_chain(rows), 25050, 3)
    assert pcr.quality.startswith("INCOMPLETE") and pcr.valid
    assert pcr.total_pcr is not None  # computed from what exists, never crashes


def test_pcr_change():
    assert pcr_change(1.23, 1.20) == 0.03
    assert pcr_change(None, 1.20) is None  # missing side stays None, never guessed


def test_pcr_trend_up():
    at = NOW
    history = [(at - timedelta(minutes=5 * (4 - i)), v) for i, v in enumerate([1.05, 1.08, 1.12, 1.18])]
    assert pcr_trend_from_history(history + [(at, 1.23)], 5, 0.02) == TrendState.UP


def test_pcr_trend_down():
    at = NOW
    history = [(at - timedelta(minutes=5 * (4 - i)), v) for i, v in enumerate([1.23, 1.18, 1.12, 1.08])]
    assert pcr_trend_from_history(history + [(at, 1.05)], 5, 0.02) == TrendState.DOWN


def test_pcr_trend_flat_within_band():
    at = NOW
    history = [(at - timedelta(minutes=5 * (4 - i)), v) for i, v in enumerate([1.10, 1.105, 1.095, 1.11])]
    assert pcr_trend_from_history(history + [(at, 1.10)], 5, 0.02) == TrendState.FLAT


def test_pcr_trend_insufficient_data():
    at = NOW
    history = [(at - timedelta(minutes=5), 1.10)]
    assert pcr_trend_from_history(history + [(at, 1.23)], 5, 0.02) == TrendState.INSUFFICIENT_DATA


# --- OI ----------------------------------------------------------------------


def test_oi_change_current_vs_previous():
    prev = compute_oi(_chain([(25050, 1000, 1200)]))
    cur = compute_oi(_chain([(25050, 1500, 1000)]), prev)
    assert prev.call_oi_change is None  # no previous snapshot: change is never guessed
    assert cur.call_oi_change == 500 and cur.put_oi_change == -200
    assert "CALL_OI_ADDED" in cur.call_interpretation
    assert "PUT_OI_UNWOUND" in cur.put_interpretation


# --- VWAP --------------------------------------------------------------------


def test_vwap_from_ohlcv():
    bars = [Bar(high=100, low=100, close=100, volume=1), Bar(high=200, low=200, close=200, volume=3)]
    assert compute_vwap(bars) == 175.0  # (100*1 + 200*3) / 4


def test_vwap_zero_volume_fallback():
    bars = [Bar(high=100, low=100, close=100, volume=0), Bar(high=200, low=200, close=200, volume=0)]
    assert compute_vwap(bars) == 150.0  # documented fallback: mean typical price


def test_vwap_distance_and_flags():
    above = vwap_features(180.0, [Bar(high=100, low=100, close=100, volume=1)])
    assert above.vwap == 100.0 and above.distance == 80.0
    assert above.above_vwap and not above.below_vwap and above.valid
    below = vwap_features(100.0, [Bar(high=200, low=200, close=200, volume=1)])
    assert below.below_vwap and not below.above_vwap


# --- Feature engine end-to-end ------------------------------------------------


def test_feature_engine_end_to_end():
    bars_raw = day_bars("e2e", "2025-01-15")[:150]  # 09:15..11:44 inclusive
    at = datetime.fromisoformat(bars_raw[-1]["timestamp"])
    snap = build_snapshot(at=at, seed="e2e", date_iso="2025-01-15")
    bars = [Bar(high=b["high"], low=b["low"], close=b["close"], volume=b["volume"]) for b in bars_raw]
    features = build_features(snap, CFG, bars=bars, pcr_history=[])
    assert features.spot == snap.tick.close
    assert features.atm.atm_strike == calculate_atm(features.spot, 50)
    assert len(features.atm.strikes) == 7
    assert features.pcr.valid and features.pcr.total_pcr is not None
    assert features.vwap.valid and features.vwap.vwap is not None
    assert features.price.available
    assert features.data_health.status == "OK"
    assert features.feature_engine_version == FEATURE_ENGINE_VERSION
    assert features.pcr.pcr_trend == TrendState.INSUFFICIENT_DATA  # no history yet
