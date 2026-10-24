"""Disposable CI stack checks. Never run fixtures against a production database."""
import json
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
    for name in ('dhan_client_id', 'dhan_access_token'):
        (folder / (name + '.txt')).write_text('')
    for path in folder.iterdir():
        path.chmod(0o644)
    (root / '.env').write_text((root / '.env.example').read_text())
    print('Disposable CI credentials generated; no Dhan token configured.')


def stack():
    import httpx
    from sqlalchemy import select, text
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


if __name__ == '__main__':
    if sys.argv[1:] == ['bootstrap']:
        bootstrap()
    elif sys.argv[1:] == ['stack']:
        stack()
    else:
        raise SystemExit('Use bootstrap or stack; CI deployment only')
