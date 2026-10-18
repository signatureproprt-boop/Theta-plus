"""Phase E — HistoricalDataSource abstraction (§5).

The replay engine consumes `iter_snapshots()` and never cares whether the data
came from SQLite, JSON, CSV or memory. All sources are offline (§41).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, Iterator, Optional, Protocol, Sequence

from data.normalizer import normalize_option_chain, normalize_tick
from models.market_models import MarketSnapshot
from storage.snapshot_store import SnapshotStore


class HistoricalDataSource(Protocol):
    """Any source of historical snapshots."""

    def iter_snapshots(self) -> Iterable[MarketSnapshot]: ...


class InMemorySource:
    def __init__(self, snapshots: Sequence[MarketSnapshot]) -> None:
        self._snapshots = list(snapshots)

    def iter_snapshots(self) -> Iterator[MarketSnapshot]:
        yield from self._snapshots


class SqliteSource:
    def __init__(
        self,
        store: SnapshotStore,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        instrument: str = "NIFTY",
    ) -> None:
        self._store, self._from, self._to, self._instrument = store, date_from, date_to, instrument

    def iter_snapshots(self) -> Iterator[MarketSnapshot]:
        yield from self._store.load(self._from, self._to, self._instrument)


class JsonFileSource:
    """JSON file holding a list of MarketSnapshot dumps."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def iter_snapshots(self) -> Iterator[MarketSnapshot]:
        data = json.loads(self.path.read_text())
        for item in data:
            yield MarketSnapshot.model_validate(item)


class CsvFileSource:
    """Flat CSV (one row per strike per timestamp) -> MarketSnapshots.

    Required columns: snapshot_id, timestamp, instrument, open, high, low, close,
    volume, strike, ce_ltp, ce_oi, ce_oi_change, ce_volume, ce_iv, pe_ltp, pe_oi,
    pe_oi_change, pe_volume, pe_iv. Rows are grouped by snapshot_id.
    """

    def __init__(self, path: Path | str, strike_interval: int = 50) -> None:
        self.path = Path(path)
        self.strike_interval = strike_interval

    def iter_snapshots(self) -> Iterator[MarketSnapshot]:
        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        with self.path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                sid = row["snapshot_id"]
                if sid not in groups:
                    groups[sid] = []
                    order.append(sid)
                groups[sid].append(row)
        for sid in order:
            rows = groups[sid]
            head = rows[0]
            tick = normalize_tick({
                "timestamp": head["timestamp"],
                "symbol": head.get("instrument", "NIFTY"),
                "open": head["open"], "high": head["high"], "low": head["low"],
                "close": head["close"], "volume": head.get("volume", 0),
            }, source="historical")
            chain_rows = [{
                "strike": r["strike"],
                "ce": {"ltp": r["ce_ltp"], "oi": r["ce_oi"], "oi_change": r.get("ce_oi_change", 0),
                       "volume": r.get("ce_volume", 0), "iv": r.get("ce_iv") or None},
                "pe": {"ltp": r["pe_ltp"], "oi": r["pe_oi"], "oi_change": r.get("pe_oi_change", 0),
                       "volume": r.get("pe_volume", 0), "iv": r.get("pe_iv") or None},
            } for r in rows]
            chain = normalize_option_chain(
                {"timestamp": head["timestamp"], "data": chain_rows,
                 "expiry": head.get("expiry", "")},
                fallback_interval=self.strike_interval, source="historical",
            )
            yield MarketSnapshot(
                snapshot_id=sid, timestamp=tick.timestamp,
                instrument=head.get("instrument", "NIFTY"),
                tick=tick, chain=chain, source="historical",
            )
