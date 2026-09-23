"""Phase A — normalizer: raw feed payloads -> immutable market models.

Never crashes on a bad payload: problems raise NormalizationError, which
callers (collectors, harness) catch and convert into WAIT / data-health
degradation. Unknown extra keys are tolerated and dropped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from lib.dates import to_ist
from models.market_models import OptionChain, OptionChainRow, OptionQuote, InstrumentTick


class NormalizationError(ValueError):
    """A payload could not be normalized into the market models."""


def _f(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"invalid {field}: {value!r}") from exc


def _i(value: Any, field: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"invalid {field}: {value!r}") from exc


def parse_timestamp(value: Any, field: str = "timestamp") -> datetime:
    """Accept ISO strings or epoch seconds; naive values are treated as IST."""
    if isinstance(value, datetime):
        return to_ist(value)
    if isinstance(value, (int, float)):
        return to_ist(datetime.fromtimestamp(float(value)))
    if isinstance(value, str):
        try:
            return to_ist(datetime.fromisoformat(value))
        except ValueError as exc:
            raise NormalizationError(f"invalid {field}: {value!r}") from exc
    raise NormalizationError(f"invalid {field}: {value!r}")


def normalize_quote(payload: Mapping[str, Any] | None) -> OptionQuote:
    """One side of a strike. Missing sub-fields default to zero/None — never crash."""
    if not isinstance(payload, Mapping):
        return OptionQuote()
    greeks = {
        k: _f(v, f"greeks.{k}")
        for k, v in payload.items()
        if k in {"delta", "gamma", "theta", "vega"} and v is not None
    }
    return OptionQuote(
        ltp=_f(payload.get("ltp", payload.get("last_price", 0.0)) or 0.0, "ltp"),
        oi=_i(payload.get("oi", 0) or 0, "oi"),
        oi_change=_i(payload.get("oi_change", 0) or 0, "oi_change"),
        volume=_i(payload.get("volume", 0) or 0, "volume"),
        iv=_f(payload["iv"], "iv") if payload.get("iv") is not None else None,
        greeks=greeks,
    )


def normalize_tick(payload: Mapping[str, Any], source: str = "dhan", symbol: str = "NIFTY") -> InstrumentTick:
    """Index quote payload -> InstrumentTick. Required: timestamp and a price."""
    if not isinstance(payload, Mapping):
        raise NormalizationError("tick payload must be a mapping")
    if "timestamp" not in payload:
        raise NormalizationError("tick payload missing timestamp")
    close = payload.get("close", payload.get("last_price", payload.get("ltp")))
    if close is None:
        raise NormalizationError("tick payload missing close/last_price")
    close = _f(close, "close")
    ohlc = payload.get("ohlc") or {}
    open_ = _f(payload.get("open", ohlc.get("open", close)), "open")
    high = _f(payload.get("high", ohlc.get("high", close)), "high")
    low = _f(payload.get("low", ohlc.get("low", close)), "low")
    vwap = payload.get("vwap")
    return InstrumentTick(
        timestamp=parse_timestamp(payload["timestamp"]),
        symbol=str(payload.get("symbol", symbol)),
        open=open_,
        high=max(high, close, open_),
        low=min(low, close, open_),
        close=close,
        volume=_i(payload.get("volume", 0) or 0, "volume"),
        vwap=_f(vwap, "vwap") if vwap is not None else None,
        source=source,
    )


def normalize_chain_rows(rows: Iterable[Mapping[str, Any]]) -> list[OptionChainRow]:
    """Option-chain row payloads -> sorted, de-duplicated OptionChainRow list."""
    by_strike: dict[int, OptionChainRow] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or "strike" not in raw:
            continue  # tolerate junk rows; validators flag incompleteness
        strike = _i(raw["strike"], "strike")
        if strike in by_strike:
            continue  # duplicate strike: keep the first occurrence
        by_strike[strike] = OptionChainRow(
            strike=strike,
            ce=normalize_quote(raw.get("ce")),
            pe=normalize_quote(raw.get("pe")),
        )
    return [by_strike[s] for s in sorted(by_strike)]


def infer_strike_interval(rows: Iterable[OptionChainRow], fallback: int) -> int:
    """Read the strike interval from chain metadata (min positive gap between
    strikes). Falls back to the configured interval when it cannot be inferred —
    the interval is never assumed to be a fixed 50."""
    strikes = sorted({r.strike for r in rows})
    gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(gaps) if gaps else fallback


def normalize_option_chain(
    payload: Mapping[str, Any],
    fallback_interval: int,
    source: str = "dhan",
    symbol: str = "NIFTY",
) -> OptionChain:
    """Option-chain payload -> OptionChain. Accepts {'data': [...]} or a bare list."""
    if isinstance(payload, Mapping):
        rows_payload = payload.get("data", payload.get("rows", []))
        timestamp_raw = payload.get("timestamp")
        expiry = str(payload.get("expiry", "") or "")
    else:
        rows_payload = payload
        timestamp_raw = None
        expiry = ""
    rows = normalize_chain_rows(rows_payload or [])
    if timestamp_raw is None:
        raise NormalizationError("option chain payload missing timestamp")
    return OptionChain(
        symbol=str(payload.get("symbol", symbol)) if isinstance(payload, Mapping) else symbol,
        timestamp=parse_timestamp(timestamp_raw),
        expiry=expiry,
        strike_interval=infer_strike_interval(rows, fallback_interval),
        rows=tuple(rows),
        source=source,
    )


def detect_duplicate_timestamp(previous: datetime | None, new: datetime) -> bool:
    """True when `new` carries no information (same instant as previous)."""
    if previous is None:
        return False
    return to_ist(previous) == to_ist(new)
