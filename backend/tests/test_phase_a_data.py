"""Phase A regression — data layer: normalizer + validators (8 tests)."""

from datetime import datetime, timedelta

from data.dhan_client import DhanMarketData
from data.normalizer import (
    detect_duplicate_timestamp,
    normalize_option_chain,
    normalize_tick,
)
from data.sim_feed import day_bars
from data.validators import chain_issues, is_stale, tick_issues
from lib.dates import IST
from models.market_models import InstrumentTick, OptionChain, OptionChainRow, OptionQuote

NOW = datetime(2025, 1, 15, 11, 45, 0, tzinfo=IST)


def test_normalize_tick_from_sim_payload():
    bars = day_bars("a", "2025-01-15")
    tick = normalize_tick(bars[60], source="sim")
    assert tick.symbol == "NIFTY"
    assert tick.close == bars[60]["close"]
    assert tick.high >= tick.low
    assert tick.timestamp.tzinfo is not None


def test_normalize_option_chain_sorts_and_dedupes_and_infers_interval():
    payload = {
        "timestamp": NOW.isoformat(),
        "data": [
            {"strike": 25100, "ce": {"last_price": 120.5, "oi": 10}, "pe": {"last_price": 80.0, "oi": 20}},
            {"strike": 25000, "ce": {"last_price": 150.0, "oi": 30}, "pe": {"last_price": 60.0, "oi": 40}},
            {"strike": 25100, "ce": {"last_price": 999.0, "oi": 1}, "pe": {"last_price": 1.0, "oi": 1}},  # duplicate
        ],
    }
    chain = normalize_option_chain(payload, fallback_interval=50)
    strikes = [r.strike for r in chain.rows]
    assert strikes == sorted(strikes)
    assert len(strikes) == len(set(strikes))
    assert chain.strike_interval == 100  # inferred from the 25000->25100 gap, not assumed


def test_stale_tick_flagged():
    tick = InstrumentTick(timestamp=NOW - timedelta(seconds=45), open=1, high=1, low=1, close=1)
    assert is_stale(tick.timestamp, NOW, 10)
    assert any("STALE" in issue for issue in tick_issues(tick, NOW, 10))


def test_invalid_prices_flagged():
    tick = InstrumentTick(timestamp=NOW, open=-5, high=10, low=1, close=0)
    issues = tick_issues(tick, NOW, 10)
    assert any("PRICE INVALID" in issue for issue in issues)


def test_zero_oi_chain_flagged_incomplete_without_crash():
    chain = OptionChain(
        timestamp=NOW,
        strike_interval=50,
        rows=(
            OptionChainRow(strike=25000, ce=OptionQuote(oi=0), pe=OptionQuote(oi=0)),
            OptionChainRow(strike=25050, ce=OptionQuote(oi=0), pe=OptionQuote(oi=0)),
        ),
    )
    issues = chain_issues(chain, NOW, 10)
    assert any("INCOMPLETE" in issue and "zero OI" in issue for issue in issues)


def test_empty_chain_flagged():
    chain = OptionChain(timestamp=NOW, strike_interval=50, rows=())
    assert any("empty option chain" in issue for issue in chain_issues(chain, NOW, 10))


def test_duplicate_timestamp_detection():
    assert detect_duplicate_timestamp(NOW, NOW)
    assert not detect_duplicate_timestamp(NOW - timedelta(minutes=1), NOW)
    assert not detect_duplicate_timestamp(None, NOW)


def test_dhan_client_is_readonly():
    """Security guard (master prompt §39): the market-data client exposes no
    order-placement surface of any kind."""
    client = DhanMarketData(access_token="x", client_id="y")
    public = [m for m in dir(client) if not m.startswith("_")]
    assert all("order" not in m.lower() for m in public)
    # Read-only market-data surface only (expiry list + nearest expiry were added
    # when the client was aligned with the current official DhanHQ v2 contract).
    assert set(public) == {
        "configured", "get_index_quote", "get_index_ltp", "get_option_chain",
        "get_expiry_list", "get_nearest_expiry", "get_index_minute_candles", "BASE_URL",
    }


def test_stale_snapshot_reports_stale_not_error():
    """REGRESSION (found during Phase D integration): a stale-but-valid feed
    must aggregate to STALE. snapshot_health used to classify the staleness
    message a second time as PRICE/INVALID, escalating STALE -> ERROR and
    mislabelling the dashboard's OVERALL STATUS."""
    from data.validators import snapshot_health
    from models.market_models import MarketSnapshot

    tick = InstrumentTick(
        timestamp=NOW - timedelta(seconds=60), open=25000, high=25010, low=24990, close=25005, volume=1000
    )
    chain = OptionChain(
        timestamp=NOW - timedelta(seconds=60),
        strike_interval=50,
        rows=(OptionChainRow(strike=25000, ce=OptionQuote(ltp=100.0, oi=1000), pe=OptionQuote(ltp=90.0, oi=1200)),),
    )
    snap = MarketSnapshot(snapshot_id="s1", timestamp=NOW, tick=tick, chain=chain)
    health = snapshot_health(snap, NOW, max_age_seconds=10)
    assert health.checks["DATA_AGE"] == "STALE"
    assert health.checks.get("PRICE", "OK") == "OK"  # prices themselves are valid
    assert health.status.value == "STALE"
