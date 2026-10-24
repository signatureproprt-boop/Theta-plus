"""HTTP adapter: never logs headers, tokens, profile bodies or raw error strings."""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
import httpx
from app.config import settings, read_secret
from app.policy import validate_identity

IST = ZoneInfo('Asia/Kolkata')
BASE = 'https://api.dhan.co/v2'


class CredentialsMissing(Exception):
    pass


def credentials():
    s = settings()
    try:
        client_id, token = read_secret(s.dhan_client_id_file), read_secret(s.dhan_access_token_file)
    except OSError:
        raise CredentialsMissing('AUTH_REQUIRED') from None
    if not client_id or not token or '\n' in token or '\r' in token:
        raise CredentialsMissing('AUTH_REQUIRED')
    return client_id, token


def auth_headers():
    client_id, token = credentials()
    return {'client-id': client_id, 'access-token': token,
            'Accept': 'application/json', 'Content-Type': 'application/json'}


def endpoint_and_payload(job, instrument):
    validate_identity(instrument.exchange_segment, instrument.instrument_type)
    payload = {'securityId': instrument.security_id,
               'exchangeSegment': instrument.exchange_segment,
               'instrument': instrument.instrument_type, 'oi': False}
    if job.interval == '1d':
        endpoint = '/charts/historical'
        payload.update(expiryCode=job.expiry_code,
                       fromDate=job.start_date.isoformat(), toDate=job.end_date.isoformat())
    else:
        endpoint = '/charts/intraday'
        payload.update(interval=job.interval,
                       fromDate=job.start_date.isoformat() + ' 00:00:00',
                       toDate=job.end_date.isoformat() + ' 00:00:00')
    return endpoint, payload


def retry_after(value, now=None):
    if not value:
        return None
    try:
        if str(value).isdigit():
            return min(int(value), 86400)
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            return None
        return max(0, min(86400, int((when - (now or datetime.now(timezone.utc))).total_seconds()) + 1))
    except (TypeError, ValueError, OverflowError):
        return None


def error_code(body):
    if not isinstance(body, dict):
        return None
    raw = body.get('errorCode', body.get('error_code', body.get('code')))
    if raw is None and isinstance(body.get('data'), dict):
        return error_code(body['data'])
    value = str(raw) if raw is not None else ''
    return value if re.fullmatch(r'(DH-\d{3}|\d{3})', value) else None


def parse_candles(body, start, end):
    keys = ('timestamp', 'open', 'high', 'low', 'close', 'volume')
    if not isinstance(body, dict) or any(not isinstance(body.get(k), list) for k in keys):
        raise ValueError('MALFORMED_RESPONSE')
    sizes = {len(body[k]) for k in keys}
    if len(sizes) != 1:
        raise ValueError('MISMATCHED_ARRAY_LENGTHS')
    candles = {}
    for values in zip(*(body[k] for k in keys)):
        stamp, *rest = values
        if isinstance(stamp, bool) or Decimal(str(stamp)) != int(stamp):
            raise ValueError('INVALID_TIMESTAMP')
        dt = datetime.fromtimestamp(int(stamp), timezone.utc)
        if not start <= dt.astimezone(IST).date() < end:
            raise ValueError('OUTSIDE_REQUESTED_RANGE')
        prices = [Decimal(str(v)) for v in rest[:4]]
        if any(not p.is_finite() or p < 0 or p >= Decimal('1e16') or
               p != p.quantize(Decimal('0.00000001')) for p in prices):
            raise ValueError('INVALID_PRICE')
        o, h, low, c = prices
        vol = Decimal(str(rest[4]))
        if not vol.is_finite() or vol != vol.to_integral_value() or not 0 <= vol <= 9223372036854775807:
            raise ValueError('INVALID_VOLUME')
        if not low <= min(o, c) <= max(o, c) <= h:
            raise ValueError('INVALID_OHLC')
        row = dict(timestamp=dt, open=o, high=h, low=low, close=c, volume=int(vol))
        if dt in candles and candles[dt] != row:
            raise ValueError('CONFLICTING_RESPONSE_CANDLES')
        candles[dt] = row
    return sorted(candles.values(), key=lambda r: r['timestamp'])


@dataclass
class FetchResult:
    outcome: str
    retryable: bool = False
    rows: list = field(default_factory=list)
    http_status: int | None = None
    code: str | None = None
    rate_evidence: bool = False
    retry_after: int | None = None
    response_hash: str | None = None
    duration_ms: int = 0
    http_started_at: datetime | None = None


def classify_response(response, start, end):
    result = FetchResult('HTTP_ERROR', http_status=response.status_code,
                         retry_after=retry_after(response.headers.get('Retry-After')),
                         response_hash=hashlib.sha256(response.content).hexdigest())
    try:
        body = json.loads(response.content, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    result.code = error_code(body)
    # Explicit provider evidence, rather than inferring rate limits from DH-907.
    result.rate_evidence = response.status_code == 429 or result.code in {'DH-904', '805'}
    if response.status_code in (401, 403) or result.code in {'DH-901', '807', '808', '809', '810'}:
        result.outcome = 'AUTH_REQUIRED'
    elif result.rate_evidence:
        result.outcome, result.retryable = 'RATE_LIMIT', True
    elif result.code == 'DH-907':
        result.outcome, result.retryable = 'DH907_REVIEW', True
    elif result.code in {'DH-902', 'DH-903', '806'}:
        result.outcome = 'ACCESS_REVIEW'
    elif response.status_code >= 500 or result.code in {'DH-908', 'DH-909', '800'}:
        result.outcome, result.retryable = 'PROVIDER_TRANSIENT', True
    elif result.code:
        result.outcome = 'PROVIDER_REVIEW'
    elif 200 <= response.status_code < 300:
        if not response.content.strip():
            result.outcome, result.retryable = 'EMPTY_RESPONSE', True
        else:
            try:
                result.rows = parse_candles(body, start, end)
                result.outcome = 'ROWS_RECEIVED' if result.rows else 'EMPTY_RESPONSE'
                result.retryable = not result.rows
            except (ValueError, TypeError, InvalidOperation, OverflowError):
                result.outcome = 'INVALID_RESPONSE'
    return result


def fetch(endpoint, payload, start, end, client=None):
    headers = auth_headers()
    started = time.monotonic()
    http_started_at = datetime.now(timezone.utc)
    try:
        if client is not None:
            response = client.post(BASE + endpoint, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as http:
                http_started_at = datetime.now(timezone.utc)
                response = http.post(BASE + endpoint, headers=headers, json=payload)
        result = classify_response(response, start, end)
    except httpx.TransportError:
        result = FetchResult('NETWORK_ERROR', retryable=True)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.http_started_at = http_started_at
    return result


def check_authentication():
    # Harmless authenticated profile request; never return personal profile data.
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as http:
            r = http.get(BASE + '/profile', headers=auth_headers())
        try:
            body = r.json()
        except ValueError:
            body = None
        return {'authenticated': bool(r.status_code == 200 and isinstance(body, dict) and body and not error_code(body)),
                'http_status': r.status_code, 'provider_code': error_code(body)}
    except CredentialsMissing:
        return {'authenticated': False, 'reason': 'AUTH_REQUIRED'}
    except httpx.TransportError:
        return {'authenticated': False, 'reason': 'NETWORK_ERROR'}
