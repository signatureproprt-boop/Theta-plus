"""Phase E — deterministic SYNTHETIC replay fixtures (§40).

Clearly labeled synthetic sessions used to PROVE replay correctness. They are
never presented as market performance; real-market validation stays PENDING
until historical Dhan data is supplied.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from lib.dates import IST
from models.market_models import (
    InstrumentTick,
    MarketSnapshot,
    OptionChain,
    OptionChainRow,
    OptionQuote,
)

INTERVAL = 50


def synthetic_session(
    date_iso: str = "2025-01-15",
    start: str = "09:15",
    minutes: int = 380,
    step_minutes: int = 5,
    base_spot: float = 25000.0,
    drift: float = 1.6,
    put_bias: bool = True,
) -> list[MarketSnapshot]:
    """One deterministic session of snapshots (no RNG).

    A steady upward drift with rising put OI makes the CE side confirm after the
    11:30 filter opens, so the replay exercises setups, cooldown and outcomes.
    """
    t0 = datetime.fromisoformat(f"{date_iso}T{start}").replace(tzinfo=IST)
    snapshots: list[MarketSnapshot] = []
    for i in range(0, minutes, step_minutes):
        ts = t0 + timedelta(minutes=i)
        spot = round(base_spot + drift * i, 2)
        atm = int(round(spot / INTERVAL)) * INTERVAL
        # PCR climbs slowly and deterministically with time (put OI added).
        put_step = 120 * i if put_bias else 0
        call_step = 20 * i if put_bias else 120 * i
        rows = []
        for k in range(-3, 4):
            strike = atm + k * INTERVAL
            ce_ltp = round(max(spot - strike, 0.0) + 60.0 + 0.55 * i, 2)  # CE gains with drift
            pe_ltp = round(max(strike - spot, 0.0) + 60.0 - 0.25 * i, 2)
            rows.append(OptionChainRow(
                strike=strike,
                ce=OptionQuote(ltp=max(ce_ltp, 1.0), oi=10_000 + call_step, oi_change=20, volume=5_000, iv=14.0),
                pe=OptionQuote(ltp=max(pe_ltp, 1.0), oi=11_000 + put_step, oi_change=120, volume=5_000, iv=14.5),
            ))
        tick = InstrumentTick(
            timestamp=ts, symbol="NIFTY",
            open=spot - 2, high=spot + 4, low=spot - 4, close=spot, volume=100_000,
            source="synthetic",
        )
        snapshots.append(MarketSnapshot(
            snapshot_id=f"syn-{date_iso}-{i:04d}",
            timestamp=ts, instrument="NIFTY", tick=tick,
            chain=OptionChain(symbol="NIFTY", timestamp=ts, strike_interval=INTERVAL,
                              rows=tuple(rows), source="synthetic"),
            source="synthetic",
        ))
    return snapshots


def synthetic_snapshot(at: datetime, spot: float = 25000.0, ce_ltp: float = 100.0,
                       pe_ltp: float = 100.0, snapshot_id: Optional[str] = None) -> MarketSnapshot:
    """A single hand-built snapshot for targeted tests."""
    atm = int(round(spot / INTERVAL)) * INTERVAL
    rows = tuple(
        OptionChainRow(
            strike=atm + k * INTERVAL,
            ce=OptionQuote(ltp=ce_ltp, oi=10_000, oi_change=10, volume=1_000, iv=14.0),
            pe=OptionQuote(ltp=pe_ltp, oi=11_000, oi_change=90, volume=1_000, iv=14.0),
        )
        for k in range(-3, 4)
    )
    tick = InstrumentTick(timestamp=at, open=spot, high=spot + 2, low=spot - 2, close=spot, volume=1_000)
    return MarketSnapshot(
        snapshot_id=snapshot_id or str(uuid.uuid4()), timestamp=at, instrument="NIFTY",
        tick=tick, chain=OptionChain(timestamp=at, strike_interval=INTERVAL, rows=rows),
        source="synthetic",
    )
