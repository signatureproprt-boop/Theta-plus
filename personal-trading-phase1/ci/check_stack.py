"""Disposable CI stack checks. Never run fixtures against a production database."""
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def bootstrap():
    root = Path(__file__).resolve().parents[1]
    folder = root / 'secrets'
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    for name in ('postgres_password', 'api_key'):
        (folder / (name + '.txt')).write_text(secrets.token_urlsafe(48))
    for name in ('dhan_client_id', 'dhan_access_token', 'legacy_admin_hash'):
        (folder / (name + '.txt')).write_text('')
    for path in folder.iterdir():
        path.chmod(0o644)
    (root / '.env').write_text((root / '.env.example').read_text())
    print('Disposable CI credentials generated; no Dhan token configured.')


def stack():
    if os.environ.get('PHASE1_DISPOSABLE_CI') != '1':
        raise SystemExit('This fixture requires an explicitly disposable CI deployment')
    for name in ('dhan_client_id', 'dhan_access_token'):
        if Path('/run/secrets/' + name).read_text().strip():
            raise SystemExit('CI fixtures refuse to run with real Dhan credentials')
    import httpx
    from sqlalchemy import select, text, func
    from app.db import session
    from app.models import FetchJob, HistoricalData
    from app.rate_gate import redis_client
    from app.tasks import save_rows
    from decimal import Decimal

    key = Path('/run/secrets/api_key').read_text().strip()
    with httpx.Client(base_url='http://127.0.0.1:8000', headers={'X-API-Key': key}, timeout=15) as http:
        for _ in range(60):
            try:
                r = http.get('/health/ready')
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise AssertionError('API readiness timeout')
        assert r.json()['mcx'] == 'HOLD'
        assert redis_client().ping()
        with session() as db:
            assert db.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='timescaledb'")) == 1
            assert db.scalar(text("SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name='historical_data'")) == 1
            tables = set(db.scalars(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")))
            assert {'instruments', 'historical_data', 'watchlist', 'fetch_jobs', 'request_attempts'} <= tables
        print('STACK_SCHEMA_REDIS_API_VERIFIED', flush=True)
        assert http.post('/auth/check').json()['authenticated'] is False
        # Synthetic identity exists ONLY inside this ephemeral GitHub CI database.
        fixture = {'security_id': '999999999999', 'symbol': 'CI_AUTH_GUARD',
                   'exchange_segment': 'IDX_I', 'instrument_type': 'INDEX', 'mapping_verified': True}
        r = http.post('/instruments', json=fixture)
        r.raise_for_status()
        request = {'security_id': fixture['security_id'], 'exchange_segment': 'IDX_I',
                   'interval': '1d', 'start': '2025-01-02', 'end': '2025-01-03'}
        r = http.post('/history/jobs', json=request)
        r.raise_for_status()
        jid = r.json()['job_ids'][0]
        # Identical registration must reuse the durable job UUID.
        assert http.post('/history/jobs', json=request).json()['job_ids'] == [jid]
        for _ in range(60):
            record = http.get('/history/jobs/' + jid).json()
            if record['status'] == 'AUTH_REQUIRED':
                break
            time.sleep(2)
        else:
            raise AssertionError('Celery/dispatcher did not process durable job')
        assert record['attempts'] == 0 and record['rows_received'] == 0
        print('STACK_CELERY_DISPATCH_AUTH_GUARD_VERIFIED', flush=True)
        # Execute real Timescale inserts, duplicate handling and conflict retention.
        # Roll back fixtures; no real provider request is made by this CI test.
        import uuid
        with session() as db:
            job = db.get(FetchJob, uuid.UUID(jid))
            row = {'timestamp': datetime(2025, 1, 1, 18, 30, tzinfo=timezone.utc),
                   'open': Decimal('100'), 'high': Decimal('105'), 'low': Decimal('99'),
                   'close': Decimal('103'), 'volume': 50}
            assert save_rows(db, job, [row]) == (1, 0)
            assert save_rows(db, job, [row]) == (0, 0)
            assert save_rows(db, job, [{**row, 'close': Decimal('104')}]) == (0, 1)
            saved = db.scalar(select(HistoricalData).where(
                HistoricalData.security_id == fixture['security_id']))
            assert saved.close == Decimal('103')
            db.rollback()
        commodity = {**fixture, 'exchange_segment': 'MCX_COMM', 'instrument_type': 'FUTCOM'}
        assert http.post('/instruments', json=commodity).status_code == 422
        print('STACK_TIMESCALE_INSERT_DEDUP_CONFLICT_AND_MCX_HOLD_VERIFIED', flush=True)
        # Additive catalogue tables and cursor planning on real PostgreSQL.
        import csv, io
        from app.market_collection import import_master, seed_progress
        from app.models import MasterIdentity, HistoryProgress, Instrument
        columns = ['EXCH_ID', 'SEGMENT', 'SECURITY_ID', 'INSTRUMENT', 'SYMBOL_NAME', 'SM_EXPIRY_DATE']
        fixture_rows = [dict(zip(columns, ['NSE', 'D', str(900000000000 + i), 'FUTSTK', 'CI_FUT_' + str(i), '2026-12-29'])) for i in range(1000)]
        def encode(rows):
            output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader(); writer.writerows(rows); return output.getvalue().encode()
        with session() as db:
            snapshot = import_master(db, encode(fixture_rows), datetime.now(timezone.utc))
            seed_progress(db, snapshot['sha256'], __import__('datetime').date(2026, 10, 24))
            assert db.scalar(select(func.count()).select_from(HistoryProgress)) == 2000
            fixture_rows[0]['SYMBOL_NAME'] = 'CHANGED_CONTRACT'
            changed = import_master(db, encode(fixture_rows), datetime.now(timezone.utc))
            assert changed['identity_conflicts'] == 1
            first = db.get(Instrument, ('NSE_FNO', '900000000000'))
            assert first.mapping_verified is False and first.review_reason == 'MASTER_IDENTITY_CHANGED'
            assert first.symbol == 'CI_FUT_0'
            db.rollback()
        print('STACK_MASTER_IMPORT_IDENTITY_REUSE_GUARD_AND_PROGRESS_VERIFIED', flush=True)


        # Unified deployment really serves the retained website and API from this container.
        home = http.get('/')
        assert home.status_code == 200 and 'desk-market-watch' in home.text
        assert http.get('/api/status').json()['dhan_configured'] is False
        assert http.get('/api/v2/system').json()['storage'] == 'PostgreSQL + TimescaleDB'
        assert http.get('/live/status').json()['owner']['state'] == 'AWAITING_VERIFIED_HANDOVER'
        print('STACK_UNIFIED_WORKSPACE_AND_HANDOVER_HOLD_VERIFIED', flush=True)
        from app.legacy_store import PostgresStore
        from app.merge_archive import import_file
        from app.models import LegacyObject, LiveTick, LiveGap, MasterSnapshot, MasterIdentity
        from app.live_runtime import persist_batch
        import hashlib
        archive = PostgresStore()
        assert archive.put_document('ci/merge', {'retained': True}, 0)
        assert not archive.put_document('ci/merge', {'retained': False}, 0)
        saved, version = archive.document('ci/merge')
        assert saved == {'retained': True}
        assert archive.put_document('ci/merge', {'retained': True, 'revision': 2}, version)
        assert not archive.put_document('ci/merge', {}, version)
        with session() as db:
            assert import_file(db, 'ci/source.json', b'{"original":true}') == 'inserted'
            db.flush()
            assert import_file(db, 'ci/source.json', b'{"original":true}') == 'unchanged'
            assert import_file(db, 'ci/source.json', b'{"original":false}') == 'updated'
            db.rollback()
        event = {'kind':'tick','event_id':'ci-unified-tick','exchange_segment':'NSE_EQ',
                 'security_id':'9999988','ltp':100,'received_at':'2025-01-02T03:45:01+00:00',
                 'trade_time':'2025-01-02T03:45:00+00:00'}
        entries = [(b'1-0', {b'event': json.dumps(event).encode()})]
        persist_batch(entries);persist_batch(entries)
        with session() as db:
            assert db.scalar(select(func.count()).select_from(LiveTick).where(LiveTick.event_id=='ci-unified-tick')) == 1
        print('STACK_ARCHIVE_CAS_AND_TICK_REPLAY_DEDUP_VERIFIED', flush=True)
        # Real PG gap runner, stubbed transport only. No provider credentials or HTTP.
        from unittest.mock import patch
        from types import SimpleNamespace
        from datetime import timedelta
        from app import dhan
        from app.gaps import process_gap
        from app.models import Instrument
        sha, fingerprint = 'f'*64, 'c'*64
        with session() as db:
            db.add(MasterSnapshot(sha256=sha, compressed_csv=b'CI_ONLY', summary={}))
            db.add(Instrument(exchange_segment='NSE_EQ',security_id='9999988',symbol='CI_LIVE',
                              instrument_type='EQUITY',mapping_verified=True,review_status='OK'))
            db.flush()
            db.add(MasterIdentity(exchange_segment='NSE_EQ',security_id='9999988',exchange='NSE',
                                  source_sha=sha,fingerprint=fingerprint,blocked=False))
            db.flush()
            gap = LiveGap(exchange_segment='NSE_EQ',security_id='9999988',fingerprint=fingerprint,
                start=datetime(2025,1,2,3,45,tzinfo=timezone.utc),end=datetime(2025,1,2,3,47,tzinfo=timezone.utc))
            db.add(gap);db.commit();gid = str(gap.id)
        class Gate:
            def __init__(self, *args): pass
            def acquire(self): return SimpleNamespace(wait_ms=0,previous_ms=1100,recent=1)
            def cooldown(self, *args): pass
        candle = {'timestamp':datetime(2025,1,2,3,45,tzinfo=timezone.utc),
            'open':Decimal('100'),'high':Decimal('101'),'low':Decimal('99'),'close':Decimal('100'),'volume':10}
        with patch('app.gaps.RateGate', Gate), patch('app.gaps.dhan.credentials', return_value=('CI_ONLY','NOT_A_TOKEN')):
            with patch('app.gaps.dhan.fetch', return_value=dhan.FetchResult('ROWS_RECEIVED',rows=[candle,{**candle,'timestamp':datetime(2025,1,2,3,47,tzinfo=timezone.utc)}])):
                assert process_gap(gid) is None
            with session() as db:
                saved = db.get(LiveGap, uuid.UUID(gid))
                assert saved.status == 'ROWS_RECEIVED' and saved.evidence[-1]['accepted_window_rows'] == 1
                assert saved.evidence[-1]['coverage_verified'] is False
                assert db.scalar(select(func.count()).select_from(HistoricalData).where(
                    HistoricalData.security_id=='9999988')) == 1
                retry_gap = LiveGap(exchange_segment='NSE_EQ',security_id='9999988',fingerprint=fingerprint,
                    start=datetime(2025,1,3,3,45,tzinfo=timezone.utc),end=datetime(2025,1,3,3,47,tzinfo=timezone.utc))
                db.add(retry_gap);db.commit();retry_id=str(retry_gap.id)
            with patch('app.gaps.dhan.fetch',return_value=dhan.FetchResult('DH907_REVIEW',retryable=True,code='DH-907',http_status=400)):
                assert process_gap(retry_id) > 0
            with session() as db:
                saved=db.get(LiveGap,uuid.UUID(retry_id))
                assert saved.status=='RETRY_WAIT' and saved.evidence[-1]['rate_limit_evidence'] is False
                assert db.get(Instrument,('NSE_EQ','9999988')).mapping_verified is True
        print('STACK_FINALIZED_GAP_WINDOW_AND_DH907_SEPARATION_VERIFIED', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['bootstrap']:
        bootstrap()
    elif sys.argv[1:] == ['stack']:
        stack()
    else:
        raise SystemExit('Use bootstrap or stack; CI deployment only')
