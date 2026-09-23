"""DhanHQ v2 payload -> normalizer-input mapping (no strategy logic here).

Shapes are taken verbatim from the current official DhanHQ v2 documentation:

  Market Quote (POST /v2/marketfeed/quote)
    {"data": {"IDX_I": {"13": {"last_price": .., "ohlc": {"open","close","high","low"},
                               "volume": .., "last_trade_time": "dd/mm/yyyy HH:MM:SS"}}}}

  Option Chain (POST /v2/optionchain)
    {"data": {"last_price": .., "oc": {"25650.000000": {"ce": {...}, "pe": {...}}}}}

`oi_change` is DERIVED as oi - previous_oi (both documented fields). When
previous_oi is absent the change stays None — it is never invented.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from lib.dates import IST
from engines.vwap_engine import Bar

QUOTE_TIME_FORMAT = "%d/%m/%Y %H:%M:%S"


class DhanPayloadError(ValueError):
    """The payload did not match the documented DhanHQ v2 contract."""


def map_minute_candles(payload: Mapping[str, Any], start: datetime, end: datetime) -> tuple[list[Bar], datetime]:
    """Validate aligned broker candles and exclude the unfinished current minute."""
    keys = ("timestamp", "high", "low", "close", "volume")
    arrays = [payload.get(key) for key in keys]
    if any(not isinstance(value, list) for value in arrays):
        raise DhanPayloadError("minute candles missing OHLCV arrays")
    if not arrays[0] or len({len(value) for value in arrays}) != 1:
        raise DhanPayloadError("minute candles have empty or misaligned arrays")
    candles: list[tuple[datetime, Bar]] = []
    for timestamp, high, low, close, volume in zip(*arrays):
        try:
            stamp = datetime.fromtimestamp(int(timestamp), IST)
            h, l, c, v = float(high), float(low), float(close), float(volume)
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            raise DhanPayloadError("minute candle contains invalid values") from exc
        if start <= stamp < end:
            if not (0 < l <= c <= h and v > 0):
                raise DhanPayloadError("minute candle has invalid price or volume")
            candles.append((stamp, Bar(high=h, low=l, close=c, volume=v)))
    if not candles or len({stamp for stamp, _ in candles}) != len(candles):
        raise DhanPayloadError("minute candles missing or duplicated")
    candles.sort(key=lambda item: item[0])
    return [bar for _, bar in candles], candles[-1][0]


def _quote_node(payload: Mapping[str, Any], security_id: str, segment: str) -> Mapping[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise DhanPayloadError("quote payload has no 'data' object")
    seg = data.get(segment)
    if not isinstance(seg, Mapping):
        raise DhanPayloadError(f"quote payload has no '{segment}' segment")
    node = seg.get(str(security_id))
    if not isinstance(node, Mapping):
        raise DhanPayloadError(f"quote payload has no security id {security_id}")
    return node


def parse_quote_time(raw: Any, fallback: Optional[datetime] = None) -> str:
    """Require a genuine broker trade time; receipt time cannot establish freshness."""
    if isinstance(raw, str) and raw.strip():
        try:
            stamp = datetime.strptime(raw.strip(), QUOTE_TIME_FORMAT).replace(tzinfo=IST)
            if stamp.year > 1990:
                return stamp.isoformat()
        except ValueError:
            pass
    raise DhanPayloadError("quote has missing or invalid last_trade_time")


def map_index_quote(
    payload: Mapping[str, Any],
    security_id: str = "13",
    segment: str = "IDX_I",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Market-quote payload -> normalize_tick() input."""
    node = _quote_node(payload, security_id, segment)
    ohlc = node.get("ohlc") if isinstance(node.get("ohlc"), Mapping) else {}
    last = node.get("last_price")
    if last is None:
        raise DhanPayloadError("quote payload has no last_price")
    return {
        "timestamp": parse_quote_time(node.get("last_trade_time"), now),
        "open": ohlc.get("open") or last,
        "high": ohlc.get("high") or last,
        "low": ohlc.get("low") or last,
        "close": last,
        "volume": int(node.get("volume") or 0),
    }


def _leg(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    oi = raw.get("oi")
    previous_oi = raw.get("previous_oi")
    oi_change: Optional[float] = None
    if oi is not None and previous_oi is not None:
        oi_change = float(oi) - float(previous_oi)
    return {
        "ltp": raw.get("last_price"),
        "oi": oi,
        "oi_change": oi_change,     # derived from documented fields only
        "volume": raw.get("volume"),
        "iv": raw.get("implied_volatility"),
        "security_id": raw.get("security_id"),
    }


def map_option_chain(
    payload: Mapping[str, Any], expiry: str = "", now: Optional[datetime] = None
) -> dict[str, Any]:
    """Option-chain payload -> normalize_option_chain() input.

    Rows with no CE and no PE data are dropped rather than zero-filled: missing
    market data must surface as missing, so the engine can answer WAIT/NO_DATA.
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise DhanPayloadError("option-chain payload has no 'data' object")
    oc = data.get("oc")
    if not isinstance(oc, Mapping) or not oc:
        raise DhanPayloadError("option-chain payload has an empty 'oc' map")

    rows: list[dict[str, Any]] = []
    for raw_strike, legs in oc.items():
        try:
            strike = float(raw_strike)
        except (TypeError, ValueError):
            continue
        if not isinstance(legs, Mapping):
            continue
        ce, pe = _leg(legs.get("ce")), _leg(legs.get("pe"))
        if not ce and not pe:
            continue
        rows.append({"strike": strike, "ce": ce, "pe": pe})

    if not rows:
        raise DhanPayloadError("option-chain payload contained no usable strikes")
    rows.sort(key=lambda r: r["strike"])
    return {
        "timestamp": (now or datetime.now(IST)).isoformat(),
        "expiry": expiry,
        "underlying_last_price": data.get("last_price"),
        "data": rows,
    }
