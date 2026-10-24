"""Composite identities prevent exchange and timeframe collisions."""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime,
                        ForeignKey, ForeignKeyConstraint, Index, Integer,
                        Numeric, String, UniqueConstraint, LargeBinary)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = 'instruments'
    exchange_segment: Mapped[str] = mapped_column(String(24), primary_key=True)
    security_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(24), nullable=False)
    mapping_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Manual review flag is independent from identity and mapping validity.
    review_status: Mapped[str] = mapped_column(String(24), default='NEEDS_MAPPING')
    review_reason: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class HistoricalData(Base):
    __tablename__ = 'historical_data'
    exchange_segment: Mapped[str] = mapped_column(String(24), primary_key=True)
    security_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(3), primary_key=True)  # 1d,1,5,15,25,60
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('fetch_jobs.id'), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(['exchange_segment', 'security_id'],
                             ['instruments.exchange_segment', 'instruments.security_id']),
        CheckConstraint("interval IN ('1d','1','5','15','25','60')"),
        CheckConstraint('volume >= 0 AND low >= 0 AND high >= low AND open BETWEEN low AND high AND close BETWEEN low AND high'),
    )


class Watchlist(Base):
    __tablename__ = 'watchlist'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    exchange_segment: Mapped[str] = mapped_column(String(24), nullable=False)
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(['exchange_segment', 'security_id'],
                             ['instruments.exchange_segment', 'instruments.security_id']),
        UniqueConstraint('exchange_segment', 'security_id'),
    )


class FetchJob(Base):
    __tablename__ = 'fetch_jobs'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange_segment: Mapped[str] = mapped_column(String(24), nullable=False)
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(3), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)  # exclusive
    expiry_code: Mapped[int] = mapped_column(Integer, default=0)
    # QUEUED/RUNNING/RETRY_WAIT/ROWS_RECEIVED/FAILED/AUTH_REQUIRED/HOLD
    status: Mapped[str] = mapped_column(String(24), default='QUEUED', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    rows_received: Mapped[int] = mapped_column(Integer, default=0)
    inserted_rows: Mapped[int] = mapped_column(Integer, default=0)
    conflicting_rows: Mapped[int] = mapped_column(Integer, default=0)
    coverage_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(String(80))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(['exchange_segment', 'security_id'],
                             ['instruments.exchange_segment', 'instruments.security_id']),
        UniqueConstraint('exchange_segment', 'security_id', 'interval', 'start_date', 'end_date', 'expiry_code'),
        CheckConstraint('end_date > start_date'),
    )


class RequestAttempt(Base):
    __tablename__ = 'request_attempts'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('fetch_jobs.id'), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    account_key: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    gate_wait_ms: Mapped[int] = mapped_column(Integer, default=0)
    since_previous_gate_ms: Mapped[int | None] = mapped_column(Integer)
    requests_in_last_second: Mapped[int] = mapped_column(Integer, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer)
    provider_code: Mapped[str | None] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(32), default='STARTED')
    rate_limit_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    retry_delay_seconds: Mapped[int | None] = mapped_column(Integer)
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    response_rows: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint('job_id', 'attempt'),
                      Index('ix_attempt_account_started', 'account_key', 'started_at'))


class SystemState(Base):
    __tablename__ = 'system_state'
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)


class MasterSnapshot(Base):
    __tablename__ = 'master_snapshots'
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    compressed_csv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)


class MasterIdentity(Base):
    __tablename__ = 'master_identities'
    exchange_segment: Mapped[str] = mapped_column(String(24), primary_key=True)
    security_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(4), nullable=False)
    source_sha: Mapped[str] = mapped_column(String(64), ForeignKey('master_snapshots.sha256'), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expiry: Mapped[date | None] = mapped_column(Date)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (ForeignKeyConstraint(['exchange_segment', 'security_id'],
                         ['instruments.exchange_segment', 'instruments.security_id']),)


class HistoryProgress(Base):
    __tablename__ = 'history_progress'
    exchange_segment: Mapped[str] = mapped_column(String(24), primary_key=True)
    security_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(3), primary_key=True)
    floor: Mapped[date] = mapped_column(Date, nullable=False)
    older_to: Mapped[date] = mapped_column(Date, nullable=False)
    recent_end: Mapped[date | None] = mapped_column(Date)
    pending_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('fetch_jobs.id'))
    state: Mapped[str] = mapped_column(String(32), default='READY', index=True)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime(1970, 1, 1, tzinfo=timezone.utc), index=True)
    __table_args__ = (ForeignKeyConstraint(['exchange_segment', 'security_id'],
                         ['instruments.exchange_segment', 'instruments.security_id']),)


class LegacyObject(Base):
    """Byte-exact retained GCS archive, plus compatibility-store CAS revisions."""
    __tablename__ = 'legacy_objects'
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    source_sha256: Mapped[str | None] = mapped_column(String(64))


class LiveTick(Base):
    __tablename__ = 'live_ticks'
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    exchange_segment: Mapped[str] = mapped_column(String(24), index=True)
    security_id: Mapped[str] = mapped_column(String(32), index=True)
    trade_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class LiveGap(Base):
    __tablename__ = 'live_gaps'
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange_segment: Mapped[str] = mapped_column(String(24))
    security_id: Mapped[str] = mapped_column(String(32))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default='QUEUED', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    __table_args__ = (ForeignKeyConstraint(['exchange_segment', 'security_id'],
                         ['instruments.exchange_segment', 'instruments.security_id']),
                     CheckConstraint('"end" > start'),
                     UniqueConstraint('exchange_segment', 'security_id', 'start', 'end'))
