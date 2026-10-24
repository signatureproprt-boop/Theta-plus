import hmac
import uuid
from datetime import date, timedelta
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from app import dhan
from app.config import read_secret, settings
from app.db import session
from app.models import FetchJob, Instrument, RequestAttempt, utcnow
from app.policy import INTERVALS, validate_identity, windows
from app.rate_gate import redis_client

app = FastAPI(title='Personal Trading — Phase 1', docs_url=None, redoc_url=None)


def require_key(x_api_key: str = Header(default='')):
    try:
        expected = read_secret(settings().api_key_file)
    except OSError:
        raise HTTPException(503, 'API_KEY_NOT_CONFIGURED') from None
    if len(expected) < 32:
        raise HTTPException(503, 'API_KEY_NOT_CONFIGURED')
    if not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, 'INVALID_API_KEY')


protected = [Depends(require_key)]


@app.get('/health/live')
def live():
    return {'alive': True}


@app.get('/health/ready')
def ready():
    try:
        with session() as db:
            db.execute(text('SELECT 1'))
        redis_client().ping()
    except Exception:
        raise HTTPException(503, 'DEPENDENCY_UNAVAILABLE') from None
    return {'database': True, 'redis': True, 'phase': 1, 'mcx': 'HOLD'}


class InstrumentInput(BaseModel):
    security_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    exchange_segment: str = Field(max_length=24)
    symbol: str = Field(min_length=1, max_length=160)
    instrument_type: str = Field(max_length=24)
    mapping_verified: bool = False

    @model_validator(mode='after')
    def identity(self):
        validate_identity(self.exchange_segment, self.instrument_type)
        return self


@app.post('/instruments', dependencies=protected, status_code=201)
def register_instrument(payload: InstrumentInput):
    with session() as db:
        data = payload.model_dump()
        data['review_status'] = 'OK' if payload.mapping_verified else 'NEEDS_MAPPING'
        stmt = insert(Instrument).values(**data).on_conflict_do_nothing()
        db.execute(stmt)
        db.commit()
        existing = db.get(Instrument, (payload.exchange_segment, payload.security_id))
        # An existing instrument cannot be silently remapped by a repeated import.
        return {k: getattr(existing, k) for k in data}


class FetchInput(BaseModel):
    exchange_segment: str
    security_id: str
    interval: Literal['1d', '1', '5', '15', '25', '60'] = '1'
    start: date
    end: date  # exclusive; only completed local dates
    expiry_code: int = Field(default=0, ge=0, le=3)

    @model_validator(mode='after')
    def dates(self):
        today = utcnow().astimezone(dhan.IST).date()
        if not date(1900, 1, 1) <= self.start < self.end <= today:
            raise ValueError('Require 1900-01-01 <= start < exclusive end <= today (IST)')
        if (self.end - self.start).days > 36600:
            raise ValueError('Range is too large')
        try:
            horizon = today.replace(year=today.year - 5)
        except ValueError:  # Leap-day anniversary in a non-leap year.
            horizon = today.replace(year=today.year - 5, day=28)
        if self.interval != '1d' and self.start < horizon:
            raise ValueError('Intraday request exceeds the five-year provider horizon')
        return self


@app.post('/history/jobs', dependencies=protected, status_code=202)
def enqueue(payload: FetchInput):
    with session() as db:
        item = db.get(Instrument, (payload.exchange_segment, payload.security_id))
        if item is None:
            raise HTTPException(404, 'REGISTER_VERIFIED_INSTRUMENT_FIRST')
        try:
            validate_identity(item.exchange_segment, item.instrument_type)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if not item.mapping_verified:
            raise HTTPException(409, 'NEEDS_MAPPING')
        ids = []
        for start, end in windows(payload.start, payload.end):
            key = dict(exchange_segment=item.exchange_segment, security_id=item.security_id,
                       interval=payload.interval, start_date=start, end_date=end,
                       expiry_code=payload.expiry_code)
            db.execute(insert(FetchJob).values(**key).on_conflict_do_nothing())
            conditions = [getattr(FetchJob, k) == v for k, v in key.items()]
            ids.append(str(db.scalar(select(FetchJob.id).where(*conditions))))
        db.commit()
    return {'job_ids': ids, 'state': 'DURABLY_REGISTERED', 'end_exclusive': True,
            'coverage_verified': False}


@app.post('/auth/check', dependencies=protected)
def auth_check():
    return dhan.check_authentication()


@app.get('/history/jobs/{jid}', dependencies=protected)
def job_status(jid: uuid.UUID):
    with session() as db:
        job = db.get(FetchJob, jid)
        if job is None:
            raise HTTPException(404, 'JOB_NOT_FOUND')
        return {c.name: getattr(job, c.name) for c in FetchJob.__table__.columns}


@app.post('/history/jobs/{jid}/retry', dependencies=protected)
def manual_retry(jid: uuid.UUID):
    # Token rotation resumes existing checkpoints without discarding attempt history.
    with session() as db:
        job = db.scalar(select(FetchJob).where(FetchJob.id == jid).with_for_update())
        if job is None:
            raise HTTPException(404, 'JOB_NOT_FOUND')
        if job.status != 'AUTH_REQUIRED':
            raise HTTPException(409, 'Only AUTH_REQUIRED jobs can resume in Phase 1; other failures require review')
        if job.attempts >= settings().max_fetch_attempts:
            raise HTTPException(409, 'ATTEMPTS_EXHAUSTED_REVIEW_REQUIRED')
        job.status, job.failure_reason = 'QUEUED', None
        job.next_attempt_at, job.last_dispatched_at = utcnow(), None
        db.commit()
    return {'status': 'QUEUED', 'attempt_history_preserved': True}


@app.get('/history/attempts', dependencies=protected)
def attempts(jid: uuid.UUID | None = None, after: int = 0,
             limit: int = Query(default=100, ge=1, le=500)):
    with session() as db:
        q = select(RequestAttempt).where(RequestAttempt.id > after)
        if jid:
            q = q.where(RequestAttempt.job_id == jid)
        rows = db.scalars(q.order_by(RequestAttempt.id).limit(limit)).all()
        return {'items': [{c.name: getattr(r, c.name) for c in RequestAttempt.__table__.columns} for r in rows],
                'next_cursor': rows[-1].id if rows else after}


@app.get('/history/error-summary', dependencies=protected)
def error_summary():
    with session() as db:
        columns = [FetchJob.exchange_segment, FetchJob.interval, RequestAttempt.http_status,
                   RequestAttempt.provider_code, RequestAttempt.outcome,
                   RequestAttempt.rate_limit_evidence]
        rows = db.execute(select(*columns, func.count().label('attempts')).join(
            RequestAttempt, RequestAttempt.job_id == FetchJob.id).group_by(*columns)).mappings().all()
        return {'groups': [dict(r) for r in rows],
                'basis': 'HTTP attempts; not unique instrument/date coverage'}
