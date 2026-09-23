"""Phase I — real historical dataset ingestion + versioning (§6/§7/§33).

Reuses the EXISTING Phase E sources (CsvFileSource / JsonFileSource /
SqliteSource). A missing required field is reported as NO_DATA — it is never
inferred, interpolated or fabricated (§41).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

from lib.dates import IST
from models.market_models import MarketSnapshot
from models.research_models import (
    ORIGIN_REAL_HISTORICAL,
    SCHEMA_VERSION,
    DatasetDescriptor,
)
from storage.sources import CsvFileSource, InMemorySource, JsonFileSource

REQUIRED_CSV_COLUMNS = (
    "snapshot_id", "timestamp", "instrument", "open", "high", "low", "close",
    "strike", "ce_ltp", "ce_oi", "pe_ltp", "pe_oi",
)
RECOMMENDED_CSV_COLUMNS = ("volume", "ce_oi_change", "pe_oi_change", "expiry",
                           "ce_volume", "pe_volume")


class RealDataError(ValueError):
    """Raised when a real dataset cannot be loaded HONESTLY (never patched)."""


def _missing_columns(header: Sequence[str]) -> list[str]:
    present = {c.strip() for c in header}
    return [c for c in REQUIRED_CSV_COLUMNS if c not in present]


def validate_csv_schema(path: Path | str) -> tuple[bool, list[str], list[str]]:
    """(ok, missing_required, missing_recommended). No file mutation."""
    p = Path(path)
    if not p.exists():
        raise RealDataError(f"NO_DATA: dataset file not found: {p.name}")
    with p.open(newline="") as fh:
        header_line = fh.readline().strip()
    if not header_line:
        raise RealDataError("NO_DATA: dataset file is empty")
    header = header_line.split(",")
    missing = _missing_columns(header)
    optional_missing = [c for c in RECOMMENDED_CSV_COLUMNS if c not in header]
    return (not missing), missing, optional_missing


def load_real_source(path: Path | str, strike_interval: int = 50):
    """Build a Phase E source for a genuine historical file (CSV or JSON)."""
    p = Path(path)
    if not p.exists():
        raise RealDataError(f"NO_DATA: dataset file not found: {p.name}")
    suffix = p.suffix.lower()
    if suffix == ".csv":
        ok, missing, _ = validate_csv_schema(p)
        if not ok:
            raise RealDataError(
                "NO_DATA: required columns absent and NOT inferred: " + ", ".join(missing)
            )
        return CsvFileSource(p, strike_interval=strike_interval)
    if suffix == ".json":
        try:
            json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            raise RealDataError(f"NO_DATA: malformed JSON dataset ({exc.msg})") from None
        return JsonFileSource(p)
    if suffix == ".parquet":
        raise RealDataError(
            "NO_DATA: parquet ingestion needs a parquet reader that is not installed; "
            "convert the dataset to CSV or JSON instead of adding a dependency"
        )
    raise RealDataError(f"NO_DATA: unsupported dataset format '{suffix}' (use .csv or .json)")


def snapshot_fingerprint(snapshot: MarketSnapshot) -> str:
    """Canonical, order-independent per-snapshot digest (no wall clock)."""
    rows = [
        f"{row.strike}|{row.ce.ltp}|{row.ce.oi}|{row.ce.oi_change}|"
        f"{row.pe.ltp}|{row.pe.oi}|{row.pe.oi_change}"
        for row in sorted(snapshot.chain.rows, key=lambda r: r.strike)
    ]
    payload = "||".join([
        snapshot.snapshot_id,
        snapshot.timestamp.isoformat(),
        snapshot.instrument,
        f"{snapshot.tick.close}",
        f"{snapshot.tick.volume}",
        *rows,
    ])
    return hashlib.sha256(payload.encode()).hexdigest()


def dataset_hash(snapshots: Iterable[MarketSnapshot]) -> str:
    digest = hashlib.sha256()
    for fp in sorted(snapshot_fingerprint(s) for s in snapshots):
        digest.update(fp.encode())
    return digest.hexdigest()


def build_descriptor(
    snapshots: Sequence[MarketSnapshot],
    source: str,
    config,
    data_origin: str = ORIGIN_REAL_HISTORICAL,
    created_at: Optional[datetime] = None,
) -> DatasetDescriptor:
    dates = sorted({s.timestamp.date().isoformat() for s in snapshots})
    dhash = dataset_hash(snapshots)
    return DatasetDescriptor(
        dataset_id=f"{data_origin.lower()}-{dhash[:12]}",
        dataset_hash=dhash,
        source=source,
        data_origin=data_origin,
        date_from=dates[0] if dates else "",
        date_to=dates[-1] if dates else "",
        trading_days=len(dates),
        snapshot_count=len(snapshots),
        created_at=created_at or datetime.now(IST),
        schema_version=SCHEMA_VERSION,
        feature_version=config.feature_engine_version,
        strategy_version=config.strategy_version,
        note=(
            "Dataset identity is content-addressed: the same file always yields the "
            "same dataset_hash, so every report is reproducible."
        ),
    )


def in_memory_source(snapshots: Sequence[MarketSnapshot]) -> InMemorySource:
    return InMemorySource(snapshots)
