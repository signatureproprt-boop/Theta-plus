"""Phase E — immutable SQLite snapshot repository (§3/§4).

Google Sheets is never the historical store. Snapshots are immutable research
inputs: a duplicate snapshot_id is DETECTED, LOGGED and skipped deterministically
(first write wins) — never overwritten, never given a random replacement id.

Raw payload and derived features are stored in separate columns (§3).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from lib.dates import to_ist
from models.market_models import MarketSnapshot

logger = logging.getLogger(__name__)

DEFAULT_DB = Path(os.environ.get("REPLAY_DB_PATH", "/app/backend/data_store/snapshots.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    timestamp     TEXT NOT NULL,
    session_date  TEXT NOT NULL,
    instrument    TEXT NOT NULL,
    spot          REAL,
    raw_payload   TEXT NOT NULL,   -- immutable raw input (tick + option chain)
    derived       TEXT,            -- derived features, kept separate from raw
    data_health   TEXT,
    strategy_version        TEXT,
    feature_engine_version  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots (session_date, timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_instrument ON snapshots (instrument, timestamp);
"""


class SnapshotStore:
    """Append-only snapshot repository."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn = sqlite3.connect(":memory:") if str(self.path) == ":memory:" else None
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self):
        if self._memory_conn is not None:
            class _Keep:  # keep the in-memory db alive across calls
                def __init__(self, c): self.c = c
                def __enter__(self): return self.c
                def __exit__(self, *a): self.c.commit()
            return _Keep(self._memory_conn)
        return closing(sqlite3.connect(str(self.path)))

    def save(
        self,
        snapshot: MarketSnapshot,
        derived: Optional[dict] = None,
        strategy_version: str = "",
        feature_engine_version: str = "",
        data_health: str = "",
    ) -> str:
        """Insert one snapshot. Returns "inserted" or "duplicate" (first wins)."""
        ts = to_ist(snapshot.timestamp)
        row = (
            snapshot.snapshot_id,
            ts.isoformat(),
            ts.date().isoformat(),
            snapshot.instrument,
            snapshot.tick.close,
            snapshot.model_dump_json(),
            json.dumps(derived) if derived else None,
            data_health,
            strategy_version,
            feature_engine_version,
            datetime.now().isoformat(),
        )
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT timestamp FROM snapshots WHERE snapshot_id = ?", (snapshot.snapshot_id,)
            ).fetchone()
            if existing:
                logger.warning(
                    "DUPLICATE_SNAPSHOT %s already stored at %s — keeping the original (immutable)",
                    snapshot.snapshot_id, existing[0],
                )
                return "duplicate"
            conn.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", row
            )
            conn.commit() if self._memory_conn is None else None
        return "inserted"

    def save_many(self, snapshots: Iterable[MarketSnapshot], **kwargs) -> dict[str, int]:
        counts = {"inserted": 0, "duplicate": 0}
        for snap in snapshots:
            counts[self.save(snap, **kwargs)] += 1
        return counts

    def count(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

    def sessions(self) -> list[str]:
        with self._conn() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT session_date FROM snapshots ORDER BY session_date"
            ).fetchall()]

    def load(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        instrument: str = "NIFTY",
    ) -> Iterator[MarketSnapshot]:
        """Chronological snapshots for a session-date range (inclusive)."""
        sql = "SELECT raw_payload FROM snapshots WHERE instrument = ?"
        params: list = [instrument]
        if date_from:
            sql += " AND session_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND session_date <= ?"
            params.append(date_to)
        sql += " ORDER BY timestamp, snapshot_id"
        with self._conn() as conn:
            for (payload,) in conn.execute(sql, params).fetchall():
                yield MarketSnapshot.model_validate_json(payload)
