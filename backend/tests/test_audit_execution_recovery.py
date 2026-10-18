"""Durable recovery tests with temporary SQLite files and mock broker only."""
import sqlite3
from datetime import timedelta
import pytest
from execution.journal import ExecutionJournal
from execution.service import ExecutionService
from models.execution_models import BrokerPosition, RECON_OK, RECON_PENDING, RECON_MISMATCH
from test_phase_h_execution import service, live_cfg, attempt, AT


def durable(monkeypatch, tmp_path, behaviour='filled'):
    svc = service(monkeypatch, live_cfg(risk_max_trades_per_day=10), behaviour=behaviour)
    svc.journal.close()
    svc.journal = ExecutionJournal(tmp_path / 'execution.sqlite')
    return svc


def restart(svc, path):
    adapter, config, instruments = svc.adapter, svc.config, svc.instruments
    svc.close()
    return ExecutionService(config, adapter=adapter, instrument_map=instruments,
                            journal=ExecutionJournal(path))


async def test_restart_preserves_dedupe_and_exposure(monkeypatch, tmp_path):
    svc = durable(monkeypatch, tmp_path)
    await attempt(svc, signal_id='durable-signal')
    fresh = restart(svc, tmp_path / 'execution.sqlite')
    assert len(fresh.orders) == 1
    assert fresh.risk_state.open_positions == 1
    assert fresh.risk_state.trades_today == 1
    assert fresh.reconciler.state == RECON_PENDING
    result = await attempt(fresh, signal_id='durable-signal')
    assert not result.approved and fresh.adapter.submit_count == 1
    fresh.close()


async def test_risk_lock_and_kill_switch_survive_restart(monkeypatch, tmp_path):
    svc = durable(monkeypatch, tmp_path)
    svc.record_realized_pnl(-50)
    svc.engage_kill_switch()
    fresh = restart(svc, tmp_path / 'execution.sqlite')
    assert fresh.kill_switch and fresh.risk_state.locked
    assert fresh.risk_state.realized_pnl_today == -50
    fresh.close()


async def test_journal_commit_failure_sends_no_order(monkeypatch, tmp_path):
    svc = durable(monkeypatch, tmp_path)
    def fail(state): raise OSError('fixture disk full')
    svc.journal.save = fail
    with pytest.raises(OSError):
        await attempt(svc, signal_id='not-submitted')
    assert svc.adapter.submit_count == 0
    assert svc.kill_switch and svc.reconciler.state == RECON_PENDING
    svc.close()


async def test_unknown_order_restart_never_blindly_resubmits(monkeypatch, tmp_path):
    svc = durable(monkeypatch, tmp_path, behaviour='timeout')
    await attempt(svc, signal_id='uncertain')
    fresh = restart(svc, tmp_path / 'execution.sqlite')
    assert await fresh.startup_reconcile() == RECON_PENDING
    assert fresh.adapter.submit_count == 1
    assert not (await attempt(fresh, signal_id='uncertain')).approved
    fresh.close()


@pytest.mark.parametrize('sid,qty,product,expected', [
    ('43492',1,'INTRADAY',RECON_OK),
    ('other',1,'INTRADAY',RECON_MISMATCH),
    ('43492',2,'INTRADAY',RECON_MISMATCH),
    ('43492',1,'MARGIN',RECON_MISMATCH)])
async def test_restart_matches_identity_quantity_product(monkeypatch,tmp_path,sid,qty,product,expected):
    svc = durable(monkeypatch,tmp_path)
    await attempt(svc,signal_id='known')
    fresh = restart(svc,tmp_path/'execution.sqlite')
    fresh.adapter.positions = [BrokerPosition(security_id=sid,exchange_segment='NSE_FNO',
        product_type=product,net_qty=qty)]
    assert await fresh.startup_reconcile() == expected
    fresh.close()


def test_journal_excludes_second_writer(tmp_path):
    path = tmp_path/'execution.sqlite'
    one = ExecutionJournal(path)
    with pytest.raises(RuntimeError): ExecutionJournal(path)
    one.close()
    two = ExecutionJournal(path)
    two.close()


def test_corrupt_journal_is_not_silently_reset(tmp_path):
    path=tmp_path/'bad.sqlite'
    path.write_bytes(b'not sqlite')
    with pytest.raises(sqlite3.DatabaseError): ExecutionJournal(path)


async def test_new_session_resets_daily_counts_but_keeps_exposure(monkeypatch,tmp_path):
    svc=durable(monkeypatch,tmp_path)
    await attempt(svc,signal_id='yesterday')
    await attempt(svc,at=AT+timedelta(days=1),signal_id='today')
    assert svc.risk_state.trades_today == 0
    assert svc.risk_state.open_positions == 1
    svc.close()


def test_real_adapter_requires_durable_journal(monkeypatch):
    svc=service(monkeypatch,live_cfg())
    svc.adapter.name='dhan'
    assert not svc.flags.live_execution_enabled
    svc.close()


async def test_journal_cannot_move_between_account_namespaces(monkeypatch,tmp_path):
    svc=durable(monkeypatch,tmp_path)
    await attempt(svc,signal_id='account-a')
    svc.close()
    monkeypatch.setenv('DHAN_CLIENT_ID','different-fixture-account')
    with pytest.raises(ValueError,match='namespace'):
        ExecutionService(svc.config,adapter=svc.adapter,instrument_map=svc.instruments,
                         journal=ExecutionJournal(tmp_path/'execution.sqlite'))
