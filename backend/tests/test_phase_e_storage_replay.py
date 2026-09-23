"""Phase E — snapshot storage, immutability, data sources, replay ordering,
session reset, determinism (§3-§10/§38)."""

import json
from datetime import datetime, timedelta

import pytest

from engines.fixtures import default_config
from lib.dates import IST
from models.market_models import MarketSnapshot
from replay.fixtures import synthetic_session, synthetic_snapshot
from replay.replay_engine import ReplayFeed, ReplayOrderingError, replay_signals
from storage.snapshot_store import SnapshotStore
from storage.sources import CsvFileSource, InMemorySource, JsonFileSource, SqliteSource

CFG = default_config()


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "snap.db")


def test_snapshot_persistence_roundtrip(store):
    snaps = synthetic_session(minutes=30, step_minutes=5)
    counts = store.save_many(snaps, strategy_version="1.0.0", feature_engine_version="1.0.0")
    assert counts == {"inserted": len(snaps), "duplicate": 0}
    assert store.count() == len(snaps)
    loaded = list(store.load())
    assert [s.snapshot_id for s in loaded] == [s.snapshot_id for s in snaps]
    assert loaded[0].tick.close == snaps[0].tick.close
    assert loaded[0].chain.rows[0].ce.oi == snaps[0].chain.rows[0].ce.oi


def test_duplicate_snapshot_detected_and_original_kept(store):
    snap = synthetic_snapshot(datetime(2025, 1, 15, 11, 30, tzinfo=IST), spot=25000.0, snapshot_id="dup-1")
    assert store.save(snap) == "inserted"
    mutated = synthetic_snapshot(datetime(2025, 1, 15, 11, 30, tzinfo=IST), spot=99999.0, snapshot_id="dup-1")
    assert store.save(mutated) == "duplicate"  # detected, logged, deterministic
    assert store.count() == 1
    assert list(store.load())[0].tick.close == 25000.0  # original preserved (immutable)


def test_raw_and_derived_kept_separately(store):
    snap = synthetic_snapshot(datetime(2025, 1, 15, 12, 0, tzinfo=IST), snapshot_id="sep-1")
    store.save(snap, derived={"total_pcr": 1.1}, data_health="OK")
    import sqlite3

    with sqlite3.connect(str(store.path)) as conn:
        raw, derived = conn.execute(
            "SELECT raw_payload, derived FROM snapshots WHERE snapshot_id='sep-1'"
        ).fetchone()
    assert "chain" in raw and json.loads(derived)["total_pcr"] == 1.1


def test_session_date_filtering(store):
    store.save_many(synthetic_session("2025-01-15", minutes=20))
    store.save_many(synthetic_session("2025-01-16", minutes=20))
    assert store.sessions() == ["2025-01-15", "2025-01-16"]
    assert all(s.timestamp.date().isoformat() == "2025-01-16"
               for s in store.load(date_from="2025-01-16"))


def test_sqlite_source_feeds_replay(store):
    store.save_many(synthetic_session(minutes=120, step_minutes=5))
    signals, digest, _ = replay_signals(SqliteSource(store).iter_snapshots(), CFG)
    assert len(signals) == 24 and digest


def test_json_and_csv_sources(tmp_path):
    snaps = synthetic_session(minutes=20, step_minutes=5)
    jpath = tmp_path / "snaps.json"
    jpath.write_text(json.dumps([json.loads(s.model_dump_json()) for s in snaps]))
    assert [s.snapshot_id for s in JsonFileSource(jpath).iter_snapshots()] == [s.snapshot_id for s in snaps]

    cpath = tmp_path / "snaps.csv"
    lines = ["snapshot_id,timestamp,instrument,open,high,low,close,volume,strike,"
             "ce_ltp,ce_oi,ce_oi_change,ce_volume,ce_iv,pe_ltp,pe_oi,pe_oi_change,pe_volume,pe_iv"]
    for s in snaps:
        for row in s.chain.rows:
            lines.append(
                f"{s.snapshot_id},{s.timestamp.isoformat()},NIFTY,{s.tick.open},{s.tick.high},"
                f"{s.tick.low},{s.tick.close},{s.tick.volume},{row.strike},{row.ce.ltp},{row.ce.oi},"
                f"{row.ce.oi_change},{row.ce.volume},14.0,{row.pe.ltp},{row.pe.oi},{row.pe.oi_change},"
                f"{row.pe.volume},14.0"
            )
    cpath.write_text("\n".join(lines))
    csv_snaps = list(CsvFileSource(cpath).iter_snapshots())
    assert len(csv_snaps) == len(snaps)
    assert len(csv_snaps[0].chain.rows) == 7
    assert csv_snaps[0].chain.strike_interval == 50


def test_replay_feed_sorts_shuffled_input_deterministically():
    snaps = synthetic_session(minutes=60, step_minutes=5)
    shuffled = [snaps[i] for i in (5, 0, 11, 3, 7, 1, 9, 2, 10, 4, 8, 6)]
    ordered = list(ReplayFeed(shuffled))
    assert [s.snapshot_id for s in ordered] == [s.snapshot_id for s in snaps]
    # Same shuffle twice -> same order (deterministic, no future info used).
    assert [s.snapshot_id for s in ReplayFeed(shuffled)] == [s.snapshot_id for s in ordered]


def test_strict_ordering_rejects_out_of_order_input():
    snaps = synthetic_session(minutes=30, step_minutes=5)
    out_of_order = [snaps[3], snaps[0]]
    with pytest.raises(ReplayOrderingError) as exc:
        ReplayFeed(out_of_order, strict=True)
    assert "precedes" in str(exc.value)


def test_shuffled_input_produces_identical_signals_after_sorting():
    snaps = synthetic_session(minutes=240, step_minutes=5)
    shuffled = list(reversed(snaps))
    a, hash_a, _ = replay_signals(snaps, CFG)
    b, hash_b, _ = replay_signals(shuffled, CFG)
    assert hash_a == hash_b
    assert [s.decision for s in a] == [s.decision for s in b]


def test_replay_is_deterministic_across_runs():
    snaps = synthetic_session(minutes=380, step_minutes=5)
    first, hash1, _ = replay_signals(snaps, CFG)
    second, hash2, _ = replay_signals(snaps, CFG)
    assert hash1 == hash2
    assert [(s.timestamp, s.decision, s.state, s.ce_score, s.pe_score, s.reason) for s in first] == \
           [(s.timestamp, s.decision, s.state, s.ce_score, s.pe_score, s.reason) for s in second]


def test_session_reset_between_days():
    day1 = synthetic_session("2025-01-15", minutes=380, step_minutes=5)
    day2 = synthetic_session("2025-01-16", minutes=380, step_minutes=5, base_spot=25000.0)
    signals, _, _ = replay_signals(day1 + day2, CFG)
    d1 = [s for s in signals if s.timestamp.date().isoformat() == "2025-01-15"]
    d2 = [s for s in signals if s.timestamp.date().isoformat() == "2025-01-16"]
    # Identical synthetic sessions -> identical first-bar VWAP: no carry-over (§8).
    assert d1[0].vwap == d2[0].vwap
    assert d1[0].vwap == pytest.approx(d1[0].spot, abs=1.0)  # first bar VWAP ~= first price


def test_pre_signal_data_is_collected_but_never_signals():
    snaps = synthetic_session(minutes=380, step_minutes=5)
    signals, _, _ = replay_signals(snaps, CFG)
    pre = [s for s in signals if s.timestamp.time().strftime("%H:%M") < "11:30"]
    assert pre, "pre-11:30 snapshots must still be evaluated to build PCR/VWAP history"
    assert all(s.decision == "WAIT" for s in pre)
    assert all("TIME_FILTER_BEFORE_START" in s.reason for s in pre)
    # ...and the accumulated history makes the trend available later in the session.
    later = [s for s in signals if s.timestamp.time().strftime("%H:%M") >= "11:30"]
    assert any(s.pcr_trend in ("UP", "DOWN", "FLAT") for s in later)
