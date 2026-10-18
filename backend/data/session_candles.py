"""Strict closed one-minute OHLCV parsing; timestamps are candle start times."""
from datetime import datetime, timedelta, timezone
import math
from engines.vwap_engine import Bar
from lib.dates import IST, to_ist


def closed_session_bars(payload: dict, now: datetime, source: str) -> list[Bar]:
    now = to_ist(now)
    columns = ('timestamp', 'open', 'high', 'low', 'close', 'volume')
    arrays = [payload.get(k) for k in columns]
    if any(not isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1:
        raise ValueError('Candle arrays missing or lengths differ')
    session_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    cutoff = min(now.replace(second=0, microsecond=0),
                 now.replace(hour=15, minute=30, second=0, microsecond=0))
    selected = {}
    for ts, op, hi, lo, cl, vol in zip(*arrays):
        at = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(IST)
        if at < session_open or at >= cutoff:
            continue
        if at.second or at.microsecond or at in selected:
            raise ValueError('Duplicate or off-grid candle')
        op, hi, lo, cl, vol = map(float, (op, hi, lo, cl, vol))
        if (not all(math.isfinite(v) for v in (op, hi, lo, cl, vol))
                or lo <= 0 or vol < 0 or not lo <= op <= hi or not lo <= cl <= hi):
            raise ValueError('Invalid OHLCV candle')
        selected[at] = Bar(hi, lo, cl, vol, source=source)
    expected = max(int((cutoff - session_open).total_seconds() // 60), 0)
    if len(selected) != expected:
        raise ValueError('Incomplete session candles; VWAP unavailable')
    return [selected[session_open + timedelta(minutes=i)] for i in range(expected)]
