"""Durable reconnect reconciliation of FINALIZED minute candles, not lost ticks."""
import json
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from celery.exceptions import Retry
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from app import dhan
from app.celery_app import celery_app
from app.config import settings
from app.db import engine, session
from app.models import (LiveGap, LiveTick, HistoricalData, MasterIdentity,
                        Instrument, FetchJob, utcnow)
from app.policy import retry_delay, validate_identity
from app.rate_gate import RateGate, GateDeferred
from app.tasks import lock_number, save_rows

LOG = logging.getLogger(__name__)
ACTIVE = ('QUEUED', 'RETRY_WAIT', 'RUNNING')


def finalized_end(now):
    # One additional minute of provider settling time; current minute never saved.
    return now.astimezone(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)


def plan_session(info):
    end = finalized_end(info.connected_at)
    floor = end - timedelta(days=7)
    planned = 0
    with session() as db:
        for seg, sid in info.instruments:
            if db.scalar(select(LiveGap.id).where(LiveGap.exchange_segment == seg,
                    LiveGap.security_id == sid, LiveGap.status.in_(ACTIVE)).limit(1)):
                continue
            item, meta = db.get(Instrument, (seg, sid)), db.get(MasterIdentity, (seg, sid))
            if not item or not item.mapping_verified or not meta or meta.blocked:
                continue
            validate_identity(seg, item.instrument_type)
            # Durable last receipt plus saved candle tail, not process-local clocks.
            tick = db.scalar(select(func.max(LiveTick.timestamp)).where(
                LiveTick.exchange_segment == seg, LiveTick.security_id == sid))
            candle = db.scalar(select(func.max(HistoricalData.timestamp)).where(
                HistoricalData.exchange_segment == seg, HistoricalData.security_id == sid,
                HistoricalData.interval == '1', HistoricalData.timestamp < end))
            candidates = [v for v in (tick, candle, info.disconnected_at) if v is not None]
            start = min(candidates) if candidates else datetime.combine(
                (end.astimezone(dhan.IST) - timedelta(days=1)).date(), time(9, 15), tzinfo=dhan.IST)
            original = start
            start = max(start, floor).astimezone(timezone.utc).replace(second=0, microsecond=0)
            if start >= end:
                continue
            # Duplicate reconnects in the same minute reuse this exact range.
            db.execute(insert(LiveGap).values(exchange_segment=seg, security_id=sid,
                start=start, end=end, fingerprint=meta.fingerprint,
                evidence=[{'event': 'PLANNED', 'generation': info.generation,
                    'bounded_to_seven_days': original < floor,
                    'coverage_verified': False, 'recoverable': 'MINUTE_OHLCV_ONLY'}])
                .on_conflict_do_nothing())
            planned += 1
        db.commit()
    return planned


def dispatch_gaps():
    with session() as db:
        clock = utcnow()
        rows = db.scalars(select(LiveGap).where(LiveGap.status.in_(ACTIVE),
            LiveGap.next_attempt_at <= clock,
            (LiveGap.last_dispatched_at.is_(None)) | (LiveGap.last_dispatched_at < clock-timedelta(minutes=5)))
            .order_by(LiveGap.next_attempt_at).limit(25).with_for_update(skip_locked=True)).all()
        for gap in rows:
            celery_app.send_task('live.reconcile', args=[str(gap.id)], queue='history')
            gap.last_dispatched_at = clock
        db.commit()
    return len(rows)


def process_gap(gap_id):
    gid = uuid.UUID(gap_id)
    with engine().connect() as conn:
        if not conn.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': lock_number(gid)}):
            conn.rollback()
            return None
        conn.commit()
        try:
            with Session(bind=conn, expire_on_commit=False) as db:
                gap = db.get(LiveGap, gid)
                if not gap or gap.status not in ACTIVE or gap.next_attempt_at > utcnow():
                    return None
                item = db.get(Instrument, (gap.exchange_segment, gap.security_id))
                meta = db.get(MasterIdentity, (gap.exchange_segment, gap.security_id))
                try:
                    validate_identity(item.exchange_segment, item.instrument_type)
                except (ValueError, AttributeError):
                    gap.status = 'HOLD'; db.commit(); return None
                if not item.mapping_verified or not meta or meta.blocked or meta.fingerprint != gap.fingerprint:
                    gap.status = 'NEEDS_MAPPING'; db.commit(); return None
                if gap.attempts >= settings().max_fetch_attempts:
                    gap.status = 'REVIEW_REQUIRED'; db.commit(); return None
                if db.scalar(text('SELECT pg_database_size(current_database())')) >= settings().history_database_soft_gib*1024**3:
                    gap.status, gap.next_attempt_at = 'RETRY_WAIT', utcnow()+timedelta(minutes=10)
                    db.commit(); return 600
                try:
                    client_id, _ = dhan.credentials()
                except dhan.CredentialsMissing:
                    gap.status = 'AUTH_REQUIRED'; db.commit(); return None
                gate = RateGate(client_id)
                try:
                    slot = gate.acquire()
                except GateDeferred as exc:
                    gap.status, gap.next_attempt_at = 'RETRY_WAIT', utcnow()+timedelta(seconds=exc.seconds)
                    db.commit(); return exc.seconds
                start_day = gap.start.astimezone(dhan.IST).date()
                end_day = (gap.end - timedelta(microseconds=1)).astimezone(dhan.IST).date()+timedelta(days=1)
                # Existing Phase 1 payload builder, parser, account gate, insertion policy.
                key = dict(exchange_segment=gap.exchange_segment, security_id=gap.security_id,
                           interval='1', start_date=start_day, end_date=end_day, expiry_code=0)
                db.execute(insert(FetchJob).values(**key, status='GAP_PROVENANCE')
                           .on_conflict_do_nothing())
                job = db.scalar(select(FetchJob).where(*(getattr(FetchJob, k)==v for k,v in key.items())))
                endpoint, payload = dhan.endpoint_and_payload(job, item)
                gap.attempts += 1
                gap.status = 'RUNNING'
                started = utcnow()
                receipt = {'event': 'STARTED', 'attempt': gap.attempts, 'endpoint': endpoint,
                    'started_at': started.isoformat(), 'gate_wait_ms': slot.wait_ms,
                    'since_previous_gate_ms': slot.previous_ms, 'requests_in_last_second': slot.recent,
                    'account_key': getattr(gate, 'account_key', None)}
                gap.evidence = [*gap.evidence, receipt]
                db.commit()  # Durable evidence before request; crash consumes an attempt.
                try:
                    result = dhan.fetch(endpoint, payload, start_day, end_day)
                except dhan.CredentialsMissing:
                    result = dhan.FetchResult('AUTH_REQUIRED')
                item = db.scalar(select(Instrument).where(Instrument.exchange_segment == gap.exchange_segment,
                    Instrument.security_id == gap.security_id).with_for_update().execution_options(populate_existing=True))
                meta = db.scalar(select(MasterIdentity).where(MasterIdentity.exchange_segment == gap.exchange_segment,
                    MasterIdentity.security_id == gap.security_id).with_for_update(read=True).execution_options(populate_existing=True))
                valid_identity = item.mapping_verified and meta and not meta.blocked and meta.fingerprint == gap.fingerprint
                rows = [r for r in result.rows if gap.start <= r['timestamp'] < gap.end]
                outcome = result.outcome
                delay = None
                if not valid_identity:
                    gap.status, outcome = 'NEEDS_MAPPING', 'IDENTITY_CHANGED_DURING_REQUEST'
                    rows = []
                elif rows:
                    added, conflicts = save_rows(db, job, rows)
                    gap.status = 'REVIEW_REQUIRED' if conflicts else 'ROWS_RECEIVED'
                    receipt.update(inserted_rows=added, conflicting_rows=conflicts)
                elif result.outcome == 'AUTH_REQUIRED':
                    gap.status = 'AUTH_REQUIRED'
                elif (result.retryable or result.rows) and gap.attempts < settings().max_fetch_attempts:
                    delay = retry_delay(gap.attempts, settings().backoff_base_seconds,
                                        settings().backoff_cap_seconds, result.retry_after)
                    gap.status, gap.next_attempt_at = 'RETRY_WAIT', utcnow()+timedelta(seconds=delay)
                    if result.rows:
                        outcome = 'EMPTY_REQUESTED_WINDOW'
                else:
                    gap.status = 'REVIEW_REQUIRED'
                receipt.update(event='FINISHED', finished_at=utcnow().isoformat(),
                    http_started_at=result.http_started_at.isoformat() if result.http_started_at else None,
                    duration_ms=result.duration_ms, http_status=result.http_status,
                    provider_code=result.code, outcome=outcome, rate_limit_evidence=result.rate_evidence,
                    retry_after_seconds=result.retry_after, retry_delay_seconds=delay,
                    response_sha256=result.response_hash, response_rows=len(result.rows),
                    accepted_window_rows=len(rows), coverage_verified=False)
                gap.evidence = [*gap.evidence[:-1], receipt]
                if gap.status == 'REVIEW_REQUIRED':
                    item.review_status, item.review_reason = 'FAILED', outcome
                db.commit()
                if result.rate_evidence:
                    gate.cooldown(max(delay or 0, result.retry_after or 60))
                LOG.info(json.dumps({'event': 'live_gap_attempt', 'gap_id': str(gid), **receipt}))
                return delay
        finally:
            conn.rollback()
            conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lock_number(gid)})
            conn.commit()


@celery_app.task(bind=True, name='live.reconcile', max_retries=None)
def reconcile(self, gap_id):
    try:
        delay = process_gap(gap_id)
        if delay is not None:
            raise self.retry(countdown=delay)
    except Retry:
        raise
    except (SQLAlchemyError, RedisError):
        raise self.retry(countdown=30)
