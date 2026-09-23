"""Focused DhanHQ integration verification (NOT a new phase, NOT the full suite).

Payload fixtures below are the response shapes published in the CURRENT official
DhanHQ v2 documentation (Market Quote, Option Chain, Expiry List) — they verify
our request/response contract handling. They are NOT market data and prove
nothing about live connectivity: that needs real credentials.

No real API call and no order is made anywhere in this file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest

from data.dhan_client import (
    MIN_INTERVAL_SECONDS,
    NIFTY_INDEX_SECURITY_ID,
    DhanDataError,
    DhanMarketData,
)
from data.dhan_instruments import build_nifty_instrument_map
from data.dhan_mapper import (
    DhanPayloadError,
    map_index_quote,
    map_minute_candles,
    map_option_chain,
    parse_quote_time,
)
from data.normalizer import normalize_option_chain, normalize_tick
from execution.instruments import InstrumentMap, validate_instrument_map
from lib.dates import IST

NOW = datetime(2025, 1, 15, 11, 45, tzinfo=IST)

# --- official documented shapes -------------------------------------------
QUOTE_PAYLOAD = {
    "data": {"IDX_I": {"13": {
        "average_price": 0, "last_price": 25642.8, "last_quantity": 0,
        "last_trade_time": "15/01/2025 11:45:00",
        "ohlc": {"open": 25500.15, "close": 25480.0, "high": 25700.5, "low": 25470.25},
        "oi": 0, "volume": 1234567, "net_change": 162.8,
    }}},
    "status": "success",
}

CHAIN_PAYLOAD = {
    "data": {
        "last_price": 25642.8,
        "oc": {
            "25600.000000": {
                "ce": {"last_price": 134.0, "oi": 3786445, "previous_oi": 3700000,
                       "volume": 117567970, "implied_volatility": 9.78, "security_id": 42528},
                "pe": {"last_price": 132.8, "oi": 3096145, "previous_oi": 3200000,
                       "volume": 157009970, "implied_volatility": 11.93, "security_id": 42529},
            },
            "25650.000000": {
                "ce": {"last_price": 108.5, "oi": 2100000, "previous_oi": 2000000,
                       "volume": 900000, "implied_volatility": 9.9, "security_id": 42530},
                "pe": {"last_price": 151.2, "oi": 2500000, "previous_oi": 2600000,
                       "volume": 800000, "implied_volatility": 12.1, "security_id": 42531},
            },
        },
    },
    "status": "success",
}


# --- 1. credentials / auth -------------------------------------------------
def test_client_is_unconfigured_without_credentials(monkeypatch):
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    client = DhanMarketData()
    assert client.configured is False


async def test_request_without_credentials_raises_and_never_calls_network(monkeypatch):
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    called = []
    monkeypatch.setattr(httpx.AsyncClient, "post",
                        lambda *a, **k: called.append(1))  # would fail if reached
    with pytest.raises(DhanDataError) as exc:
        await DhanMarketData().get_index_quote()
    assert "credentials missing" in str(exc.value)
    assert called == []


def test_auth_headers_match_the_official_contract():
    client = DhanMarketData(access_token="T", client_id="C")
    headers = client._headers()
    assert headers["access-token"] == "T" and headers["client-id"] == "C"
    assert headers["Accept"] == "application/json"


def test_client_has_no_order_surface():
    for banned in ("place_order", "submit_order", "cancel_order", "modify_order", "orders"):
        assert not hasattr(DhanMarketData, banned)


# --- 2. verified request contract -----------------------------------------
async def _capture(monkeypatch, coro_factory, response: dict):
    seen: dict = {}

    class Resp:
        status_code = 200

        def json(self):
            return response

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["body"] = json
            seen["headers"] = headers
            return Resp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setitem(MIN_INTERVAL_SECONDS, "marketfeed", 0.0)
    monkeypatch.setitem(MIN_INTERVAL_SECONDS, "optionchain", 0.0)
    result = await coro_factory()
    return seen, result


async def test_index_quote_uses_marketfeed_quote_endpoint(monkeypatch):
    client = DhanMarketData(access_token="T", client_id="C")
    seen, _ = await _capture(monkeypatch, lambda: client.get_index_quote(), QUOTE_PAYLOAD)
    assert seen["url"].endswith("/v2/marketfeed/quote")   # /ltp has no OHLC or volume
    assert seen["body"] == {"IDX_I": [13]}


async def test_option_chain_body_matches_official_fields(monkeypatch):
    client = DhanMarketData(access_token="T", client_id="C")
    seen, _ = await _capture(
        monkeypatch, lambda: client.get_option_chain(expiry="2025-01-30"), CHAIN_PAYLOAD)
    assert seen["url"].endswith("/v2/optionchain")
    assert seen["body"] == {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I",
                            "Expiry": "2025-01-30"}


async def test_expiry_list_endpoint_and_nearest_expiry(monkeypatch):
    client = DhanMarketData(access_token="T", client_id="C")
    payload = {"data": ["2025-01-09", "2025-01-16", "2025-01-30"], "status": "success"}
    seen, expiries = await _capture(monkeypatch, lambda: client.get_expiry_list(), payload)
    assert seen["url"].endswith("/v2/optionchain/expirylist")
    assert seen["body"] == {"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"}
    assert expiries == payload["data"]
    # nearest non-past expiry is selected from Dhan's own list, never guessed
    assert await client.get_nearest_expiry(on=datetime(2025, 1, 10).date()) == "2025-01-16"


async def test_empty_expiry_list_is_an_error_not_a_guess(monkeypatch):
    client = DhanMarketData(access_token="T", client_id="C")
    with pytest.raises(DhanDataError):
        await _capture(monkeypatch, lambda: client.get_expiry_list(), {"data": []})


def test_rate_limits_match_the_documented_values():
    assert MIN_INTERVAL_SECONDS["marketfeed"] >= 1.0      # 1 request / second
    assert MIN_INTERVAL_SECONDS["optionchain"] >= 3.0     # 1 request / 3 seconds


# --- 3. failure handling ---------------------------------------------------
async def test_http_error_becomes_dhan_data_error(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    monkeypatch.setitem(MIN_INTERVAL_SECONDS, "marketfeed", 0.0)
    with pytest.raises(DhanDataError):
        await DhanMarketData(access_token="T", client_id="C").get_index_quote()


async def test_non_2xx_and_non_json_are_contained(monkeypatch):
    class Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body

        def json(self):
            if self._body is None:
                raise ValueError("not json")
            return self._body

    for code, body in ((401, {"errorType": "Invalid_Authentication"}), (200, None)):
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return Resp(code, body)

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setitem(MIN_INTERVAL_SECONDS, "marketfeed", 0.0)
        with pytest.raises(DhanDataError):
            await DhanMarketData(access_token="T", client_id="C").get_index_quote()


def test_errors_never_leak_the_token():
    client = DhanMarketData(access_token="SECRET-TOKEN", client_id="CID-9")
    try:
        client._headers()
    except DhanDataError as exc:  # pragma: no cover - configured, so no raise
        assert "SECRET-TOKEN" not in str(exc)
    blob = json.dumps({k: v for k, v in vars(client).items() if not k.startswith("_")})
    assert "SECRET-TOKEN" not in blob and "CID-9" not in blob


# --- 4. payload mapping into the EXISTING normalizer ----------------------
def test_quote_payload_maps_to_tick_with_ohlc_volume_and_ist_timestamp():
    tick = normalize_tick(map_index_quote(QUOTE_PAYLOAD, now=NOW), source="dhan")
    assert tick.close == 25642.8 and tick.open == 25500.15
    assert tick.high == 25700.5 and tick.low == 25470.25
    assert tick.volume == 1234567
    assert tick.timestamp.utcoffset() == timedelta(hours=5, minutes=30)
    assert tick.timestamp.hour == 11 and tick.timestamp.minute == 45


def test_epoch_zero_trade_time_is_rejected_instead_of_fabricating_freshness():
    with pytest.raises(DhanPayloadError, match="last_trade_time"):
        parse_quote_time("01/01/1980 00:00:00", fallback=NOW)
    with pytest.raises(DhanPayloadError, match="last_trade_time"):
        map_index_quote({"data": {"IDX_I": {"13": {"last_price": 25000}}}}, now=NOW)


async def test_feed_refreshes_expiry_list_after_rollover():
    from engines.fixtures import default_config
    from pipeline import DhanFeed

    feed = DhanFeed(default_config())
    feed._expiry = "2025-01-14"

    class ExpiryProbe:
        def __init__(self):
            self.calls = []

        async def get_expiry_list(self):
            self.calls.append("refresh")

        async def get_nearest_expiry(self, on):
            self.calls.append("nearest")
            raise DhanDataError("stop after expiry selection")

    feed.client = ExpiryProbe()
    with pytest.raises(DhanDataError):
        await feed.get_snapshot(NOW)
    assert feed.client.calls == ["refresh", "nearest"]


def test_explicit_dhan_source_does_not_silently_use_simulation(monkeypatch):
    from engines.fixtures import default_config
    from pipeline import build_feed

    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    with pytest.raises(DhanDataError, match="requires"):
        build_feed(default_config().model_copy(update={"data_source": "dhan"}))


def test_vwap_candles_require_real_aligned_positive_volume():
    from datetime import timedelta

    start = NOW.replace(hour=9, minute=15)
    raw = {"timestamp": [int((NOW - timedelta(minutes=1)).timestamp())],
           "high": [25100], "low": [25000], "close": [25050], "volume": [100]}
    bars, latest = map_minute_candles(raw, start, NOW)
    assert len(bars) == 1 and bars[0].volume == 100
    assert latest == NOW - timedelta(minutes=1)
    with pytest.raises(DhanPayloadError, match="volume"):
        map_minute_candles({**raw, "volume": [0]}, start, NOW)
    with pytest.raises(DhanPayloadError, match="misaligned"):
        map_minute_candles({**raw, "close": []}, start, NOW)


def test_option_chain_payload_maps_to_chain_with_derived_oi_change():
    chain = normalize_option_chain(
        map_option_chain(CHAIN_PAYLOAD, expiry="2025-01-30", now=NOW),
        fallback_interval=50, source="dhan",
    )
    assert chain.expiry == "2025-01-30"
    assert chain.strike_interval == 50            # inferred from the payload
    assert [r.strike for r in chain.rows] == [25600, 25650]
    first = chain.rows[0]
    assert first.ce.ltp == 134.0 and first.ce.oi == 3786445
    assert first.ce.oi_change == 86445.0          # oi - previous_oi
    assert first.pe.oi_change == -103855.0
    assert first.pe.iv == 11.93


def test_missing_previous_oi_leaves_oi_change_unknown_instead_of_zero():
    payload = {"data": {"last_price": 1.0, "oc": {"25600.000000": {
        "ce": {"last_price": 10.0, "oi": 100}, "pe": {"last_price": 9.0, "oi": 200}}}}}
    mapped = map_option_chain(payload, expiry="2025-01-30", now=NOW)
    assert mapped["data"][0]["ce"]["oi_change"] is None


def test_broken_payloads_are_rejected_not_defaulted():
    for bad in ({}, {"data": {}}, {"data": {"oc": {}}}):
        with pytest.raises(DhanPayloadError):
            map_option_chain(bad, now=NOW)
    for bad in ({}, {"data": {"IDX_I": {}}}, {"data": {"IDX_I": {"13": {}}}}):
        with pytest.raises(DhanPayloadError):
            map_index_quote(bad, now=NOW)


def test_real_data_pipeline_reaches_a_decision_without_touching_strategy_code():
    """REAL-shaped Dhan payloads -> existing normalizer -> feature + signal engine."""
    from engines.feature_engine import build_features
    from engines.signal_engine import evaluate
    from engines.signal_memory import SignalMemory
    from lib.config import get_config
    from engines.vwap_engine import Bar
    from models.market_models import MarketSnapshot

    config = get_config()
    tick = normalize_tick(map_index_quote(QUOTE_PAYLOAD, now=NOW), source="dhan")
    chain = normalize_option_chain(
        map_option_chain(CHAIN_PAYLOAD, expiry="2025-01-30", now=NOW),
        fallback_interval=50, source="dhan")
    snapshot = MarketSnapshot(snapshot_id="dhan-1", timestamp=NOW, instrument="NIFTY",
                              tick=tick, chain=chain, source="dhan")
    bars = [Bar(high=tick.high, low=tick.low, close=tick.close, volume=float(tick.volume))]
    features = build_features(snapshot, config, bars=bars)
    decision = evaluate(features, config, SignalMemory().snapshot(), NOW)
    assert decision.decision in ("CE_SETUP", "PE_SETUP", "WAIT")
    assert features.atm.atm_strike in (25600, 25650)
    assert decision.strategy_version == "1.0.0"   # strategy untouched


# --- 5. instrument mapping from the official scrip master -----------------
SCRIP_SAMPLE = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,"
    "SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
    "SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME\n"
    "NSE,I,13,INDEX,0,NIFTY,1.0,Nifty 50,0001-01-01,,XX,0.0500,,INDEX,X,NIFTY\n"
    "NSE,D,48704,FUTIDX,0,NIFTY-Jan2025-FUT,75.0,NIFTY JAN FUT,2025-01-30 14:30:00,,XX,0.05,M,FUTIDX,,NIFTY\n"
    "NSE,D,57828,OPTIDX,0,NIFTY-Jan2025-25050-CE,75.0,NIFTY 30 JAN 25050 CALL,"
    "2025-01-30 14:30:00,25050.00000,CE,0.05,M,OPTIDX,,NIFTY\n"
    "NSE,D,57829,OPTIDX,0,NIFTY-Jan2025-25050-PE,75.0,NIFTY 30 JAN 25050 PUT,"
    "2025-01-30 14:30:00,25050.00000,PE,0.05,M,OPTIDX,,NIFTY\n"
    "NSE,D,99999,OPTIDX,0,BANKNIFTY-Jan2025-50000-CE,15.0,BANKNIFTY CALL,"
    "2025-01-30 14:30:00,50000.00000,CE,0.05,M,OPTIDX,,BANKNIFTY\n"
)


def test_instrument_map_is_built_from_the_official_master_and_validates():
    mapping = build_nifty_instrument_map(
        SCRIP_SAMPLE, on=datetime(2025, 1, 15).date())
    assert mapping["source_type"] == "OFFICIAL_DHAN_SCRIP_MASTER"
    assert mapping["underlying_security_id"] == "13"
    assert mapping["expiry"] == "2025-01-30"
    assert mapping["broker_verified"] is False     # not proven at the broker yet
    kinds = {i["kind"] for i in mapping["instruments"]}
    assert kinds == {"NIFTY", "NIFTY FUTURE", "NIFTY CE", "NIFTY PE"}
    # NIFTY only: no other underlying leaks into the map
    assert all(i["underlying"] == "NIFTY" for i in mapping["instruments"])
    assert not any("BANKNIFTY" in i["trading_symbol"] for i in mapping["instruments"])

    report = validate_instrument_map(mapping["instruments"])
    assert report["status"] == "PASS" and report["errors"] == ()

    resolved = InstrumentMap(mapping["instruments"]).resolve("NIFTY", 25050, "CE")
    assert resolved is not None
    assert (resolved.security_id, resolved.exchange_segment, resolved.lot_size,
            resolved.expiry) == ("57828", "NSE_FNO", 75, "2025-01-30")


def test_expired_only_master_is_rejected_rather_than_guessed():
    with pytest.raises(Exception):
        build_nifty_instrument_map(SCRIP_SAMPLE, on=datetime(2026, 1, 1).date())


def test_configured_env_map_is_the_one_the_execution_layer_uses():
    """The live env-configured map must resolve real NIFTY option contracts."""
    import os

    from dotenv import load_dotenv

    load_dotenv("/app/backend/.env")  # the runtime reads the map path from here
    path = os.environ.get("DHAN_INSTRUMENT_MAP_PATH", "")
    if not path or not os.path.exists(path):
        pytest.skip("DHAN_INSTRUMENT_MAP_PATH not configured in this environment")
    data = json.loads(open(path).read())
    report = validate_instrument_map(data["instruments"])
    assert report["status"] == "PASS"
    assert data["source_type"] == "OFFICIAL_DHAN_SCRIP_MASTER"
    strikes = sorted({i["strike"] for i in data["instruments"] if i["strike"]})
    assert len(strikes) > 10
    ref = InstrumentMap.from_env().resolve("NIFTY", strikes[len(strikes) // 2], "CE")
    assert ref is not None and ref.security_id and ref.lot_size > 0


# --- 6. safety: real data never arms execution ---------------------------
def test_live_execution_defaults_stay_off_with_real_data_configured():
    from lib.config import get_config

    config = get_config()
    assert config.live_execution_enabled is False
    assert config.live_execution_armed is False
    assert config.system_mode in ("RESEARCH", "PAPER")
