import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
import fakeredis
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql
from app import dhan, rate_gate
from app.config import Settings, settings
from app.main import app, require_key
from app.models import Base, HistoricalData, Instrument
from app.policy import retry_delay, validate_identity, windows
from app.rate_gate import GateDeferred, RateGate
from app.tasks import mark_failed

START, END = date(2026, 10, 21), date(2026, 10, 24)
STAMP = int(datetime(2026, 10, 21, 3, 45, tzinfo=timezone.utc).timestamp())


def candle(**overrides):
    return dict(timestamp=[STAMP], open=[100], high=[105], low=[99], close=[103], volume=[50], **overrides)


def response(body=None, status=200, headers=None, content=None):
    return httpx.Response(status, json=body, headers=headers) if content is None else httpx.Response(status, content=content, headers=headers)


@pytest.mark.parametrize('status,code,outcome,retry,evidence', [
    (400, 'DH-907', 'DH907_REVIEW', True, False),
    (200, 'DH-907', 'DH907_REVIEW', True, False),
    (429, None, 'RATE_LIMIT', True, True),
    (400, 'DH-904', 'RATE_LIMIT', True, True),
    (200, '805', 'RATE_LIMIT', True, True),
    (401, None, 'AUTH_REQUIRED', False, False),
    (400, 'DH-901', 'AUTH_REQUIRED', False, False),
    (200, '807', 'AUTH_REQUIRED', False, False),
    (400, 'DH-902', 'ACCESS_REVIEW', False, False),
    (400, '813', 'PROVIDER_REVIEW', False, False),
    (400, 'DH-905', 'PROVIDER_REVIEW', False, False),
    (503, None, 'PROVIDER_TRANSIENT', True, False),
    (400, 'DH-909', 'PROVIDER_TRANSIENT', True, False),
])
def test_error_classification_does_not_guess_rate_limit(status, code, outcome, retry, evidence):
    r = dhan.classify_response(response({'errorCode': code}, status), START, END)
    assert (r.outcome, r.retryable, r.rate_evidence) == (outcome, retry, evidence)


def test_empty_arrays_and_blank_response_retry_without_invalidating_identity():
    for r in [response({k: [] for k in candle()}), response(content=b'')]:
        result = dhan.classify_response(r, START, END)
        assert result.outcome == 'EMPTY_RESPONSE' and result.retryable
    instrument = SimpleNamespace(mapping_verified=True, review_status='OK', review_reason=None)
    job = SimpleNamespace()
    mark_failed(job, instrument, 'EMPTY_RESPONSE')
    assert job.status == 'FAILED' and instrument.review_status == 'FAILED'
    assert instrument.mapping_verified is True


def test_valid_response_preserves_epoch_and_decimal_values():
    result = dhan.classify_response(response(candle()), START, END)
    assert result.outcome == 'ROWS_RECEIVED' and not result.retryable
    assert result.rows[0]['timestamp'].timestamp() == STAMP
    assert result.rows[0]['open'] == Decimal('100')


@pytest.mark.parametrize('change', [
    {'volume': []}, {'high': [90]}, {'low': [-1]}, {'volume': [1.5]},
    {'timestamp': [STAMP + 20 * 86400]}, {'close': [float('nan')]},
])
def test_malformed_or_invalid_candles_not_written(change):
    body = candle()
    body.update(change)
    r = dhan.classify_response(response(content=json.dumps(body).encode()), START, END)
    assert r.outcome == 'INVALID_RESPONSE' and not r.rows


def test_conflicting_duplicate_candles_are_rejected():
    body = {k: v * 2 for k, v in candle().items()}
    body['close'][1] = 104
    with pytest.raises(ValueError, match='CONFLICTING'):
        dhan.parse_candles(body, START, END)


def test_nested_error_is_not_misread_as_empty_success():
    r = dhan.classify_response(response({'status': 'failure', 'data': {'error_code': 'DH-907'}}), START, END)
    assert r.outcome == 'DH907_REVIEW'


def test_retry_after_supports_seconds_and_http_dates():
    now = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)
    assert dhan.retry_after('90', now) == 90
    assert dhan.retry_after('Sat, 24 Oct 2026 00:01:00 GMT', now) >= 60
    assert dhan.retry_after('garbage', now) is None


def test_exponential_backoff_is_bounded_and_respects_retry_after():
    for attempt in range(1, 9):
        cap = min(300, 5 * 2 ** (attempt - 1))
        for _ in range(10):
            assert max(1, cap // 2) <= retry_delay(attempt, 5, 300) <= cap
    assert retry_delay(1, 5, 300, 600) == 600


def test_90_day_chunks_have_no_date_holes_or_overlap():
    slices = list(windows(date(2025, 1, 1), date(2026, 1, 1)))
    assert slices[0][0] == date(2025, 1, 1) and slices[-1][1] == date(2026, 1, 1)
    assert all((b - a).days <= 90 for a, b in slices)
    assert all(a[1] == b[0] for a, b in zip(slices, slices[1:]))


@pytest.mark.parametrize('segment,kind', [('MCX_COMM', 'FUTCOM'), ('MCX_COMM', 'OPTFUT'), ('NSE_COMM', 'OPTFUT')])
def test_commodity_hold_cannot_be_bypassed(segment, kind):
    with pytest.raises(ValueError, match='COMMODITY_HOLD'):
        validate_identity(segment, kind)


def test_ambiguous_mapping_is_rejected():
    with pytest.raises(ValueError, match='NEEDS_MAPPING'):
        validate_identity('NSE_EQ', 'FUTIDX')


def test_daily_and_intraday_wire_format():
    item = SimpleNamespace(security_id='1333', exchange_segment='NSE_EQ', instrument_type='EQUITY')
    job = SimpleNamespace(interval='1d', start_date=START, end_date=END, expiry_code=0)
    endpoint, payload = dhan.endpoint_and_payload(job, item)
    assert endpoint == '/charts/historical' and payload['expiryCode'] == 0
    assert payload['toDate'] == '2026-10-24'
    job.interval = '1'
    endpoint, payload = dhan.endpoint_and_payload(job, item)
    assert endpoint == '/charts/intraday' and payload['interval'] == '1'
    assert 'expiryCode' not in payload and payload['toDate'] == '2026-10-24 00:00:00'


def test_secrets_read_fresh_without_logging_them(tmp_path, monkeypatch, caplog):
    client_id, token = tmp_path / 'client.txt', tmp_path / 'token.txt'
    client_id.write_text('123456')
    token.write_text('first-private-token')
    monkeypatch.setattr(dhan, 'settings', lambda: SimpleNamespace(
        dhan_client_id_file=str(client_id), dhan_access_token_file=str(token)))
    assert dhan.auth_headers()['access-token'] == 'first-private-token'
    token.write_text('second-private-token')
    assert dhan.auth_headers()['access-token'] == 'second-private-token'
    assert 'private-token' not in caplog.text


def test_transport_timeout_returns_retryable_evidence(monkeypatch):
    monkeypatch.setattr(dhan, 'auth_headers', lambda: {'access-token': 'test'})
    def timeout(request):
        raise httpx.ReadTimeout('simulated timeout', request=request)
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        r = dhan.fetch('/charts/intraday', {}, START, END, client)
    assert r.outcome == 'NETWORK_ERROR' and r.retryable and not r.rate_evidence
    assert r.http_started_at is not None


def test_atomic_shared_account_spacing():
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeRedis(server=server)
    first = RateGate('same-account', redis).acquire()
    second = RateGate('same-account', redis).acquire()
    assert first.previous_ms is None
    assert second.previous_ms >= settings().dhan_min_spacing_ms


def test_parallel_workers_share_one_gate(monkeypatch):
    monkeypatch.setattr(rate_gate, 'settings', lambda: Settings(dhan_min_spacing_ms=250))
    redis = fakeredis.FakeRedis()
    with ThreadPoolExecutor(max_workers=3) as pool:
        slots = list(pool.map(lambda _: RateGate('parallel-account', redis).acquire(), range(3)))
    assert sum(s.previous_ms is None for s in slots) == 1
    assert all(s.previous_ms is None or s.previous_ms >= 250 for s in slots)


def test_cooldown_blocks_other_workers():
    redis = fakeredis.FakeRedis()
    RateGate('account', redis).cooldown(60)
    with pytest.raises(GateDeferred) as exc:
        RateGate('account', redis).acquire()
    assert exc.value.seconds >= 59


def test_daily_guard_defers_without_provider_call(monkeypatch):
    monkeypatch.setattr(rate_gate, 'settings', lambda: Settings(dhan_daily_guard=1))
    redis = fakeredis.FakeRedis()
    RateGate('account', redis).acquire()
    with pytest.raises(GateDeferred) as exc:
        RateGate('account', redis).acquire()
    assert exc.value.seconds >= 86399


def test_postgres_schema_separates_segments_and_intervals():
    assert [c.name for c in Instrument.__table__.primary_key] == ['exchange_segment', 'security_id']
    assert {c.name for c in HistoricalData.__table__.primary_key} == {'exchange_segment', 'security_id', 'interval', 'timestamp'}
    for table in Base.metadata.sorted_tables:
        assert 'CREATE TABLE' in str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_api_rejects_missing_credentials_before_touching_database(tmp_path, monkeypatch):
    import app.main as main
    key = tmp_path / 'key'
    key.write_text('x' * 64)
    monkeypatch.setattr(main, 'settings', lambda: SimpleNamespace(api_key_file=str(key)))
    with TestClient(app) as client:
        assert client.post('/history/jobs', json={}).status_code == 401


def test_api_blocks_commodity_registration():
    app.dependency_overrides[require_key] = lambda: None
    try:
        with TestClient(app) as client:
            r = client.post('/instruments', json={'security_id': '123', 'symbol': 'GOLD',
                'exchange_segment': 'MCX_COMM', 'instrument_type': 'FUTCOM'})
            assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_compose_only_api_has_a_loopback_port():
    compose = yaml.safe_load((Path(__file__).parents[1] / 'compose.yaml').read_text())
    services = compose['services']
    assert services['api']['ports'][0].startswith('127.0.0.1:')
    assert not services['postgres'].get('ports') and not services['redis'].get('ports')
    assert services['worker']['depends_on']['migrate']['condition'] == 'service_completed_successfully'
    assert services['redis']['command'][-1] == 'noeviction'
