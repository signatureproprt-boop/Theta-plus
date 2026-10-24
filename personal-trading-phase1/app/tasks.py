import hashlib
import json
import logging
import uuid
from datetime import timedelta
from celery.exceptions import Retry
from redis.exceptions import RedisError
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app import dhan
from app.celery_app import celery_app
from app.config import settings
from app.db import engine
from app.models import FetchJob, HistoricalData, Instrument, RequestAttempt, utcnow, MasterIdentity
from app.policy import retry_delay, validate_identity
from app.rate_gate import GateDeferred, RateGate

log = logging.getLogger(__name__)
ACTIVE = {'QUEUED', 'RETRY_WAIT', 'RUNNING'}


def lock_number(jid):
    return int.from_bytes(hashlib.sha256(str(jid).encode()).digest()[:8], 'big', signed=True)


def mark_failed(job, instrument, reason):
    job.status, job.failure_reason = 'FAILED', reason
    instrument.review_status, instrument.review_reason = 'FAILED', reason
    # No delete, delist, validity or mapping change, even after exhausted retries.


def save_rows(db, job, rows):
    inserted = conflicts = 0
    for offset in range(0, len(rows), 500):
        batch = rows[offset:offset + 500]
        payload = [dict(**row, security_id=job.security_id,
                        exchange_segment=job.exchange_segment, interval=job.interval,
                        source_job_id=job.id) for row in batch]
        stmt = insert(HistoricalData).values(payload).on_conflict_do_nothing(
            index_elements=['exchange_segment', 'security_id', 'interval', 'timestamp'])
        inserted += len(db.execute(stmt.returning(HistoricalData.timestamp)).all())
        existing = db.scalars(select(HistoricalData).where(
            HistoricalData.exchange_segment == job.exchange_segment,
            HistoricalData.security_id == job.security_id,
            HistoricalData.interval == job.interval,
            HistoricalData.timestamp.in_([r['timestamp'] for r in batch]))).all()
        by_time = {r.timestamp: r for r in existing}
        for row in batch:
            saved = by_time[row['timestamp']]
            conflicts += any(getattr(saved, k) != row[k] for k in ('open', 'high', 'low', 'close', 'volume'))
    return inserted, conflicts


def process_job(job_id):
    """Returns retry delay. PostgreSQL session lock fences duplicate deliveries."""
    jid = uuid.UUID(job_id)
    with engine().connect() as conn:
        locked = conn.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': lock_number(jid)})
        conn.commit()
        if not locked:
            return None
        try:
            with Session(bind=conn, expire_on_commit=False) as db:
                job = db.get(FetchJob, jid)
                if job is None or job.status not in ACTIVE or job.next_attempt_at > utcnow():
                    return None
                instrument = db.get(Instrument, (job.exchange_segment, job.security_id))
                try:
                    validate_identity(instrument.exchange_segment, instrument.instrument_type)
                except ValueError as exc:
                    job.status = 'HOLD' if str(exc) == 'COMMODITY_HOLD' else 'FAILED'
                    job.failure_reason = str(exc)
                    db.commit()
                    return None
                if not instrument.mapping_verified:
                    job.status, job.failure_reason = 'FAILED', 'NEEDS_MAPPING'
                    instrument.review_status = 'NEEDS_MAPPING'
                    db.commit()
                    return None
                if job.attempts >= settings().max_fetch_attempts:
                    mark_failed(job, instrument, 'ATTEMPTS_EXHAUSTED')
                    db.commit()
                    return None
                try:
                    client_id, _ = dhan.credentials()
                except dhan.CredentialsMissing:
                    job.status, job.failure_reason = 'AUTH_REQUIRED', 'AUTH_REQUIRED'
                    db.commit()
                    return None
                gate = RateGate(client_id)
                if db.scalar(text('SELECT pg_database_size(current_database())')) >= settings().history_database_soft_gib * 1024 ** 3:
                    job.status, job.failure_reason = 'RETRY_WAIT', 'STORAGE_SOFT_LIMIT'
                    job.next_attempt_at = utcnow() + timedelta(minutes=10)
                    db.commit()
                    return 600
                try:
                    slot = gate.acquire()  # Redis failure means no HTTP call.
                except GateDeferred as exc:
                    job.status = 'RETRY_WAIT'
                    job.next_attempt_at = utcnow() + timedelta(seconds=exc.seconds)
                    db.commit()
                    return exc.seconds
                endpoint, payload = dhan.endpoint_and_payload(job, instrument)
                metadata_before = db.get(MasterIdentity, (job.exchange_segment, job.security_id))
                fingerprint_before = metadata_before.fingerprint if metadata_before else None
                # A previous process may have died mid-request. Keep that evidence.
                db.execute(update(RequestAttempt).where(
                    RequestAttempt.job_id == jid, RequestAttempt.outcome == 'STARTED'
                ).values(outcome='INTERRUPTED_UNKNOWN'))
                job.attempts += 1
                job.status = 'RUNNING'
                attempt = RequestAttempt(job_id=jid, attempt=job.attempts,
                    endpoint=endpoint, account_key=gate.account_key,
                    started_at=utcnow(), gate_wait_ms=slot.wait_ms,
                    since_previous_gate_ms=slot.previous_ms,
                    requests_in_last_second=slot.recent)
                db.add(attempt)
                db.commit()  # Durable STARTED receipt before contacting Dhan.
                try:
                    result = dhan.fetch(endpoint, payload, job.start_date, job.end_date)
                except dhan.CredentialsMissing:
                    result = dhan.FetchResult('AUTH_REQUIRED')
                received = len(result.rows)
                # Recheck under shared row locks before saving: a concurrent
                # master refresh must not reinterpret a reused derivative ID.
                instrument = db.scalar(select(Instrument).where(
                    Instrument.exchange_segment == job.exchange_segment,
                    Instrument.security_id == job.security_id).with_for_update()
                    .execution_options(populate_existing=True))
                metadata_after = db.scalar(select(MasterIdentity).where(
                    MasterIdentity.exchange_segment == job.exchange_segment,
                    MasterIdentity.security_id == job.security_id).with_for_update(read=True)
                    .execution_options(populate_existing=True))
                if (not instrument.mapping_verified or (metadata_after and metadata_after.blocked) or
                        (fingerprint_before and (not metadata_after or metadata_after.fingerprint != fingerprint_before))):
                    result.rows, result.outcome, result.retryable = [], 'IDENTITY_CHANGED_DURING_REQUEST', False
                attempt.finished_at = utcnow()
                attempt.http_started_at = result.http_started_at
                attempt.duration_ms = result.duration_ms
                attempt.http_status, attempt.provider_code = result.http_status, result.code
                attempt.outcome, attempt.rate_limit_evidence = result.outcome, result.rate_evidence
                attempt.retry_after_seconds = result.retry_after
                attempt.response_sha256, attempt.response_rows = result.response_hash, received
                delay = None
                if result.rows:
                    added, conflicts = save_rows(db, job, result.rows)
                    job.rows_received = len(result.rows)
                    job.inserted_rows, job.conflicting_rows = added, conflicts
                    if conflicts:
                        mark_failed(job, instrument, 'SAVED_CANDLE_CONFLICT')
                    else:
                        job.status, job.failure_reason = 'ROWS_RECEIVED', None
                    # Nonempty response alone never verifies date/session coverage.
                    job.coverage_verified = False
                elif result.outcome == 'AUTH_REQUIRED':
                    job.status, job.failure_reason = 'AUTH_REQUIRED', result.outcome
                elif result.retryable and job.attempts < settings().max_fetch_attempts:
                    s = settings()
                    delay = retry_delay(job.attempts, s.backoff_base_seconds,
                                        s.backoff_cap_seconds, result.retry_after)
                    job.status, job.failure_reason = 'RETRY_WAIT', result.outcome
                    job.next_attempt_at = utcnow() + timedelta(seconds=delay)
                    attempt.retry_delay_seconds = delay
                else:
                    mark_failed(job, instrument, result.outcome)
                db.commit()
                # A cooldown applies account-wide, including the terminal attempt.
                if result.rate_evidence:
                    try:
                        gate.cooldown(max(delay or 0, result.retry_after or 60))
                    except RedisError:
                        # Future calls still fail closed at acquire() if Redis is down.
                        log.warning('RATE_COOLDOWN_STORE_UNAVAILABLE job=%s', jid)
                log.info(json.dumps({'event': 'dhan_attempt', 'job_id': str(jid),
                    'segment': job.exchange_segment, 'security_id': job.security_id,
                    'from': job.start_date.isoformat(), 'to_exclusive': job.end_date.isoformat(),
                    'interval': job.interval, 'attempt': job.attempts,
                    'started_at': attempt.started_at.isoformat(), 'duration_ms': result.duration_ms,
                    'http_started_at': result.http_started_at.isoformat() if result.http_started_at else None,
                    'since_previous_gate_ms': slot.previous_ms, 'gate_wait_ms': slot.wait_ms,
                    'http_status': result.http_status, 'provider_code': result.code,
                    'rate_limit_evidence': result.rate_evidence, 'outcome': result.outcome,
                    'rows': len(result.rows), 'next_delay_seconds': delay}))
                return delay
        finally:
            conn.rollback()
            conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lock_number(jid)})
            conn.commit()


@celery_app.task(bind=True, name='history.fetch', max_retries=None)
def fetch_history(self, job_id):
    try:
        delay = process_job(job_id)
        if delay is not None:
            raise self.retry(countdown=delay)
    except Retry:
        raise
    except (SQLAlchemyError, RedisError):
        # Transport retries don't label the instrument invalid. Durable dispatcher
        # also republishes pending work if this retry cannot reach the broker.
        log.warning('INFRASTRUCTURE_RETRY job=%s', job_id)
        raise self.retry(countdown=min(300, 5 * 2 ** min(self.request.retries, 6)))
