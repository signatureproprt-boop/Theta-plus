"""Phase A — SIMULATED market feed (development / research harness only).

Clearly labeled simulation: NEVER passed off as real market data. Deterministic
per (seed, session date) — the same session date always produces the same day,
which is what makes the research harness reproducible. Unit tests use their own
hand-built fixtures and do not depend on this module.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from typing import Optional

from data.normalizer import normalize_option_chain, normalize_tick
from lib.dates import IST, to_ist
from models.market_models import MarketSnapshot

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"


def day_bars(seed: str, date_iso: str, base: float = 25000.0) -> list[dict]:
    """Deterministic 1-minute OHLCV bars for one session (09:15..15:30 IST)."""
    rng = random.Random(f"nifty-{seed}-{date_iso}")
    open_t = datetime.fromisoformat(f"{date_iso}T{MARKET_OPEN}").replace(tzinfo=IST)
    close_t = datetime.fromisoformat(f"{date_iso}T{MARKET_CLOSE}").replace(tzinfo=IST)
    minutes = int((close_t - open_t).total_seconds() // 60)
    bars: list[dict] = []
    price = base
    for i in range(minutes):
        ts = open_t + timedelta(minutes=i)
        minute_of_day = ts.hour * 60 + ts.minute
        # Regime shape: choppy open, quieter midday, directional afternoon.
        if minute_of_day < 10 * 60 + 30:
            drift = rng.uniform(-3.0, 3.0)
        elif minute_of_day < 14 * 60:
            drift = rng.uniform(-1.5, 1.5)
        else:
            drift = rng.uniform(0.5, 3.5) if rng.random() < 0.6 else rng.uniform(-2.0, 0.5)
        open_p = price
        close_p = round(price + drift, 2)
        high = round(max(open_p, close_p) + rng.uniform(0.5, 4.0), 2)
        low = round(min(open_p, close_p) - rng.uniform(0.5, 4.0), 2)
        bars.append(
            {
                "timestamp": ts.isoformat(),
                "open": open_p,
                "high": high,
                "low": low,
                "close": close_p,
                "volume": int(rng.uniform(80_000, 220_000)),
            }
        )
        price = close_p
    return bars


def chain_payload(at: datetime, spot: float, interval: int = 50, spread: int = 6, seed: str = "harness") -> dict:
    """Dhan-shaped option-chain payload around `spot` (put-biased like real index)."""
    rng = random.Random(f"chain-{seed}-{at.isoformat()}")
    atm = int(round(spot / interval)) * interval
    rows = []
    for k in range(-spread, spread + 1):
        strike = atm + k * interval
        ce_intr = max(spot - strike, 0.0)
        pe_intr = max(strike - spot, 0.0)
        tv = max(40.0 - abs(spot - strike) * 0.05, 6.0)
        base_oi = 12_000 - abs(k) * 700
        put_oi = max(base_oi + (2_000 if k <= 0 else 0), 500)
        call_oi = max(base_oi + (2_000 if k >= 0 else 0), 500)
        rows.append(
            {
                "strike": strike,
                "ce": {
                    "last_price": round(ce_intr + tv + rng.uniform(-2, 2), 2),
                    "oi": call_oi,
                    "oi_change": int(rng.uniform(-800, 900)),
                    "volume": int(rng.uniform(20_000, 90_000)),
                    "iv": round(rng.uniform(11.0, 17.0), 2),
                },
                "pe": {
                    "last_price": round(pe_intr + tv + rng.uniform(-2, 2), 2),
                    "oi": put_oi,
                    "oi_change": int(rng.uniform(-700, 1_000)),
                    "volume": int(rng.uniform(20_000, 90_000)),
                    "iv": round(rng.uniform(11.0, 17.0), 2),
                },
            }
        )
    return {"symbol": "NIFTY", "timestamp": at.isoformat(), "expiry": "", "data": rows}


def build_snapshot(at: Optional[datetime] = None, seed: str = "harness", date_iso: Optional[str] = None) -> MarketSnapshot:
    """One simulated snapshot (tick + chain) at `at` (default: now, IST)."""
    at = to_ist(at) if at else datetime.now(IST)
    date_iso = date_iso or at.date().isoformat()
    bars = day_bars(seed, date_iso)
    open_t = to_ist(datetime.fromisoformat(f"{bars[0]['timestamp']}"))
    minute_index = int((at - open_t).total_seconds() // 60)
    bar = bars[max(min(minute_index, len(bars) - 1), 0)]
    # Stamp the quote AS OF `at`: a live quote carries the current instant, while
    # the bar supplies its OHLCV. Using the bar's own minute timestamp would make
    # every simulated snapshot look up to 60s stale against max_data_age_seconds.
    tick = normalize_tick({**bar, "timestamp": at.isoformat(), "symbol": "NIFTY"}, source="sim")
    chain = normalize_option_chain(chain_payload(at, tick.close, seed=seed), fallback_interval=50, source="sim")
    return MarketSnapshot(
        snapshot_id=str(uuid.uuid4()),
        timestamp=at,
        instrument="NIFTY",
        tick=tick,
        chain=chain,
        source="sim",
    )
