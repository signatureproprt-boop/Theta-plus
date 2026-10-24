"""Pinned NSE/BSE master and bounded historical plans. No commodity mapping."""
import csv
import hashlib
import io
import json
import logging
import re
import zlib
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import httpx
from sqlalchemy import and_, case, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert
from app.config import settings
from app.db import session
from app.dhan import IST, credentials, CredentialsMissing
from app.models import (FetchJob, HistoryProgress, Instrument, MasterIdentity,
                        MasterSnapshot, SystemState, utcnow)
from app.policy import validate_identity

MASTER = 'https://images.dhan.co/api-data/api-scrip-master-detailed.csv'
SEGMENTS = {('NSE', 'E'): 'NSE_EQ', ('BSE', 'E'): 'BSE_EQ',
            ('NSE', 'D'): 'NSE_FNO', ('BSE', 'D'): 'BSE_FNO',
            ('NSE', 'I'): 'IDX_I', ('BSE', 'I'): 'IDX_I',
            ('NSE', 'C'): 'NSE_CURRENCY', ('BSE', 'C'): 'BSE_CURRENCY'}
log = logging.getLogger(__name__)


def put_state(db, key, data):
    stmt = insert(SystemState).values(key=key, data=data)
    db.execute(stmt.on_conflict_do_update(index_elements=['key'], set_={'data': data}))


def parse_master(content):
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    required = {'EXCH_ID', 'SEGMENT', 'SECURITY_ID', 'INSTRUMENT', 'SYMBOL_NAME', 'SM_EXPIRY_DATE'}
    if not required <= set(reader.fieldnames or []):
        raise ValueError('MASTER_SCHEMA_REVIEW')
    rows = {}; counts = Counter()
    for raw in reader:
        counts['source_rows'] += 1
        exchange, segment = raw['EXCH_ID'].strip(), raw['SEGMENT'].strip()
        # Do not resolve, probe or automate any commodity identity.
        if exchange == 'MCX' or segment == 'M':
            counts['commodity_rows_on_hold'] += 1; continue
        api_segment = SEGMENTS.get((exchange, segment))
        kind, sid = raw['INSTRUMENT'].strip(), raw['SECURITY_ID'].strip()
        if not api_segment or not re.fullmatch(r'[1-9][0-9]{0,19}', sid):
            counts['other_mapping_review_rows'] += 1; continue
        try:
            validate_identity(api_segment, kind)
            expiry = None
            if segment in ('C', 'D'):
                expiry = date.fromisoformat(raw['SM_EXPIRY_DATE'].strip()[:10])
                if not date(1900, 1, 1) <= expiry <= date(2199, 12, 31):
                    raise ValueError('EXPIRY_REVIEW')
        except ValueError:
            counts['other_mapping_review_rows'] += 1; continue
        symbol = raw['SYMBOL_NAME'].strip()
        if not symbol or len(symbol) > 160:
            counts['other_mapping_review_rows'] += 1; continue
        # Protect old candles from reused derivative security IDs.
        identity = [exchange, segment, sid, kind, raw.get('ISIN', '')]
        if expiry is not None:
            identity += [symbol, expiry.isoformat(), raw.get('STRIKE_PRICE', ''),
                         raw.get('OPTION_TYPE', ''), raw.get('UNDERLYING_SECURITY_ID', '')]
        fingerprint = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        row = dict(exchange=exchange, exchange_segment=api_segment, security_id=sid, symbol=symbol,
                   instrument_type=kind, expiry=expiry, fingerprint=fingerprint)
        key = (api_segment, sid)
        if key in rows and rows[key]['fingerprint'] != fingerprint:
            raise ValueError('MASTER_AMBIGUOUS_SECURITY_ID')
        rows[key] = row
    if not rows:
        raise ValueError('EMPTY_SUPPORTED_MASTER')
    counts['supported_nse_bse_identities'] = len(rows)
    return list(rows.values()), dict(counts)


def download_master():
    content = bytearray()
    with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
        with client.stream('GET', MASTER) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 128 * 1024 ** 2:
                    raise ValueError('MASTER_DOWNLOAD_BOUND')
    return bytes(content)


def import_master(db, content, clock):
    rows, summary = parse_master(content)
    if len(rows) < 1000:
        raise ValueError('MASTER_SUSPICIOUSLY_SMALL')
    sha = hashlib.sha256(content).hexdigest()
    if db.get(MasterSnapshot, sha) is None:
        db.add(MasterSnapshot(sha256=sha, fetched_at=clock, compressed_csv=zlib.compress(content), summary=summary))
        db.flush()
    existing = {(r.exchange_segment, r.security_id): r for r in db.scalars(select(Instrument)).all()}
    metadata = {(r.exchange_segment, r.security_id): r for r in db.scalars(select(MasterIdentity)).all()}
    instruments = []; identities = []; conflicts = 0
    for row in rows:
        key = (row['exchange_segment'], row['security_id'])
        old, previous = existing.get(key), metadata.get(key)
        conflict = bool(old and old.instrument_type != row['instrument_type']) or bool(previous and previous.fingerprint != row['fingerprint'])
        if conflict:
            conflicts += 1
            old.mapping_verified, old.review_status, old.review_reason = False, 'NEEDS_MAPPING', 'MASTER_IDENTITY_CHANGED'
        elif old:
            old.mapping_verified = True
            if old.review_status == 'NEEDS_MAPPING' and old.review_reason != 'MASTER_IDENTITY_CHANGED':
                old.review_status, old.review_reason = 'OK', None
        else:
            instruments.append({k: row[k] for k in ('exchange_segment', 'security_id', 'symbol', 'instrument_type')} |
                               {'mapping_verified': True, 'review_status': 'OK'})
        identities.append(dict(exchange=row['exchange'], exchange_segment=key[0], security_id=key[1], source_sha=sha,
                               fingerprint=previous.fingerprint if previous else row['fingerprint'],
                               expiry=previous.expiry if conflict and previous else row['expiry'], blocked=conflict))
    for offset in range(0, len(instruments), 500):
        db.execute(insert(Instrument).values(instruments[offset:offset + 500]).on_conflict_do_nothing())
    db.flush()
    for offset in range(0, len(identities), 500):
        stmt = insert(MasterIdentity).values(identities[offset:offset + 500])
        db.execute(stmt.on_conflict_do_update(index_elements=['exchange_segment', 'security_id'],
                   set_={k: getattr(stmt.excluded, k) for k in ('source_sha', 'fingerprint', 'expiry', 'blocked')}))
    state = {**summary, 'sha256': sha, 'checked_at': clock.isoformat(), 'identity_conflicts': conflicts,
             'commodity_mapping': 'HOLD', 'coverage_verified': False}
    put_state(db, 'nse-bse-master', state)
    return state


def target_end(clock):
    local = clock.astimezone(IST)
    return local.date() - timedelta(days=1 if local.hour < 2 else 0)


def five_years_before(day):
    try:
        return day.replace(year=day.year - 5)
    except ValueError:
        return day.replace(year=day.year - 5, day=28)


def next_range(progress, end, expiry=None, exchange='NSE'):
    if expiry:
        end = min(end, expiry + timedelta(days=1))
    # First request is a small recent probe. Older slices remain durable work.
    if progress.recent_end is None:
        from app.calendar_evidence import session as market_session
        start = end - timedelta(days=1)
        # This only widens the request across weekends; it is not a coverage
        # assertion or a substitute for the correct exchange calendar.
        while start.weekday() >= 5 or market_session(start, exchange)['state'] == 'CLOSED':
            start -= timedelta(days=1)
        if market_session(start, exchange)['state'] != 'REGULAR':
            start = min(start, end - timedelta(days=7))
        return max(progress.floor, start), end
    if progress.recent_end < end:
        return progress.recent_end, min(end, progress.recent_end + timedelta(days=7))
    if progress.older_to > progress.floor:
        size = 90 if progress.interval == '1d' else 7
        return max(progress.floor, progress.older_to - timedelta(days=size)), progress.older_to
    return None


def seed_progress(db, sha, end):
    for interval, floor in [('1d', date(1900, 1, 1)), ('1', five_years_before(end))]:
        q = select(MasterIdentity.exchange_segment, MasterIdentity.security_id, literal(interval),
                   literal(floor), literal(end), literal('READY'),
                   literal(datetime(1970, 1, 1, tzinfo=timezone.utc))).where(
                   MasterIdentity.source_sha == sha, MasterIdentity.blocked.is_(False))
        # Expired contracts remain catalogue records; active-contract intraday
        # requests are the documented supported scope.
        if interval != '1d':
            q = q.where((MasterIdentity.expiry.is_(None)) | (MasterIdentity.expiry >= end))
        missing = ~select(HistoryProgress.security_id).where(
            HistoryProgress.exchange_segment == MasterIdentity.exchange_segment,
            HistoryProgress.security_id == MasterIdentity.security_id,
            HistoryProgress.interval == interval).exists()
        q = q.where(missing).order_by(case((MasterIdentity.exchange_segment == 'IDX_I', 0),
            (MasterIdentity.exchange_segment.in_(['NSE_EQ', 'BSE_EQ']), 1), else_=2),
            MasterIdentity.exchange_segment, MasterIdentity.security_id).limit(2000)
        stmt = insert(HistoryProgress).from_select(
            ['exchange_segment', 'security_id', 'interval', 'floor', 'older_to', 'state', 'planned_at'], q)
        db.execute(stmt.on_conflict_do_nothing())


def advance(progress, job):
    if job.status == 'ROWS_RECEIVED':
        progress.older_to = min(progress.older_to, job.start_date)
        progress.recent_end = max(progress.recent_end or job.end_date, job.end_date)
        progress.pending_job_id, progress.state = None, 'READY'
    elif job.status in ('FAILED', 'HOLD'):
        progress.state = 'REVIEW_REQUIRED'
    elif job.status == 'AUTH_REQUIRED':
        progress.state = 'WAITING_AUTH'


def maintain(clock=None):
    s = settings()
    if not s.collect_nse_bse:
        return
    clock = clock or utcnow()
    with session() as db:
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(73462002)')):
            return
        previous = db.get(SystemState, 'nse-bse-master')
        if previous is None or datetime.fromisoformat(previous.data['checked_at']).astimezone(IST).date() < clock.astimezone(IST).date():
            master = import_master(db, download_master(), clock)
        else:
            master = previous.data
        put_state(db, 'nse-bse-maintenance-error', {'state': 'OK', 'checked_at': clock.isoformat()})
        end = target_end(clock)
        seed_progress(db, master['sha256'], end)
        state = {'checked_at': clock.isoformat(), 'enabled': True, 'commodity_mapping': 'HOLD',
                 'catalogue_sha256': master['sha256'], 'intervals': ['1d', '1'], 'coverage_verified': False}
        try:
            cid, token = credentials()
            revision = hashlib.sha256((cid + ':' + token).encode()).hexdigest()
            old = db.get(SystemState, 'credential-revision')
            if old is None or old.data.get('sha256') != revision:
                db.execute(update(FetchJob).where(FetchJob.status == 'AUTH_REQUIRED', FetchJob.attempts < s.max_fetch_attempts)
                           .values(status='QUEUED', next_attempt_at=clock, last_dispatched_at=None))
                put_state(db, 'credential-revision', {'sha256': revision})
        except CredentialsMissing:
            state['state'] = 'AUTH_REQUIRED'
            put_state(db, 'nse-bse-collection', state);db.commit();return
        active_pairs = db.execute(select(HistoryProgress, FetchJob).join(
            FetchJob, HistoryProgress.pending_job_id == FetchJob.id).where(
                HistoryProgress.state.in_(['WAITING', 'WAITING_AUTH']))).all()
        for progress, job in active_pairs:
            advance(progress, job)
        if db.scalar(select(func.count()).select_from(FetchJob).where(FetchJob.status == 'AUTH_REQUIRED')):
            state['state'] = 'AUTH_REQUIRED'
        elif db.scalar(text('SELECT pg_database_size(current_database())')) >= s.history_database_soft_gib * 1024 ** 3:
            state['state'] = 'STORAGE_SOFT_LIMIT'
        elif s.parallel_budget_required and not __import__('app.parallel_budget', fromlist=['ready']).ready():
            state['state'] = 'WAITING_FOR_SHARED_ACCOUNT_BUDGET'
        else:
            pending = db.scalar(select(func.count()).select_from(FetchJob).where(FetchJob.status.in_(['QUEUED', 'RUNNING', 'RETRY_WAIT'])))
            available = max(0, min(100, s.history_queue_bound - pending))
            progress_rows = db.scalars(select(HistoryProgress).join(Instrument, and_(
                Instrument.exchange_segment == HistoryProgress.exchange_segment, Instrument.security_id == HistoryProgress.security_id))
                .join(MasterIdentity, and_(MasterIdentity.exchange_segment == HistoryProgress.exchange_segment,
                                           MasterIdentity.security_id == HistoryProgress.security_id))
                .where(HistoryProgress.state == 'READY', Instrument.mapping_verified.is_(True),
                       MasterIdentity.source_sha == master['sha256'], MasterIdentity.blocked.is_(False))
                .order_by(HistoryProgress.planned_at, case((HistoryProgress.exchange_segment == 'IDX_I', 0),
                    (HistoryProgress.exchange_segment.in_(['NSE_EQ', 'BSE_EQ']), 1), else_=2),
                    HistoryProgress.exchange_segment, HistoryProgress.security_id, HistoryProgress.interval)
                .limit(available).with_for_update(of=HistoryProgress, skip_locked=True)).all() if available else []
            queued = 0
            for progress in progress_rows:
                metadata = db.get(MasterIdentity, (progress.exchange_segment, progress.security_id))
                if progress.interval == '1' and metadata.expiry and metadata.expiry < end:
                    progress.state = 'EXPIRED_CONTRACT_REVIEW';continue
                if progress.interval == '1':
                    progress.floor = max(progress.floor, five_years_before(end))
                    progress.older_to = max(progress.older_to, progress.floor)
                exchange = metadata.exchange if not progress.exchange_segment.endswith('CURRENCY') else 'UNKNOWN'
                span = next_range(progress, end, metadata.expiry, exchange)
                if not span or span[0] >= span[1]:
                    progress.planned_at = clock;continue
                key = dict(exchange_segment=progress.exchange_segment, security_id=progress.security_id,
                           interval=progress.interval, start_date=span[0], end_date=span[1], expiry_code=0)
                db.execute(insert(FetchJob).values(**key).on_conflict_do_nothing())
                job = db.scalar(select(FetchJob).where(*(getattr(FetchJob, k) == v for k, v in key.items())))
                progress.pending_job_id, progress.state, progress.planned_at = job.id, 'WAITING', clock
                advance(progress, job)
                queued += 1
            state.update(state='RUNNING', queued_this_scan=queued, active_queue=pending,
                         queue_bound=s.history_queue_bound, scope='Mapped NSE/BSE cash, indices, derivatives and currency; daily and 1-minute. Current master only; commodities excluded.')
        put_state(db, 'nse-bse-collection', state)
        db.commit()
        log.info('NSE_BSE_COLLECTION %s', json.dumps(state))
