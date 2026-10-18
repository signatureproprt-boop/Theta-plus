"""Validated, timestamped inputs. No market fixtures enter the runtime."""
from __future__ import annotations
from datetime import datetime, date, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

UTC = timezone.utc

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')

def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError('Timestamps must include a timezone offset')
    return value.astimezone(UTC)

class Candle(Model):
    timestamp: datetime  # start of a one-minute candle
    open: FiniteFloat = Field(gt=0)
    high: FiniteFloat = Field(gt=0)
    low: FiniteFloat = Field(gt=0)
    close: FiniteFloat = Field(gt=0)
    volume: int = Field(default=0, ge=0)
    _aware = field_validator('timestamp')(aware)

    @model_validator(mode='after')
    def prices(self):
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValueError('Invalid OHLC bounds')
        return self

class Contract(Model):
    security_id: str = Field(min_length=1, max_length=60)
    expiry: date
    strike: FiniteFloat = Field(gt=0)
    side: Literal['CE', 'PE']
    lot_size: int | None = Field(default=None, gt=0, le=100000)
    bid: FiniteFloat = Field(ge=0)
    ask: FiniteFloat = Field(ge=0)
    bid_qty: int = Field(default=0, ge=0)
    ask_qty: int = Field(default=0, ge=0)
    oi: int = Field(ge=0)
    volume: int = Field(ge=0)
    iv: FiniteFloat | None = Field(default=None, ge=0)
    delta: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    quote_at: datetime | None = None
    @field_validator('quote_at')
    @classmethod
    def quote_time(cls, value):
        return aware(value) if value else None

class Snapshot(Model):
    timestamp: datetime
    symbol: Literal['NIFTY'] = 'NIFTY'
    spot: FiniteFloat = Field(gt=0)
    source: str = Field(min_length=1, max_length=120)
    contracts: list[Contract] = Field(min_length=1, max_length=500)
    _aware = field_validator('timestamp')(aware)

    @model_validator(mode='after')
    def unique(self):
        keys=[(c.expiry,c.strike,c.side) for c in self.contracts]
        ids=[c.security_id for c in self.contracts]
        if len(set(keys)) != len(keys) or len(set(ids)) != len(ids):
            raise ValueError('Duplicate option contracts in snapshot')
        if any(c.quote_at and c.quote_at > self.timestamp for c in self.contracts):
            raise ValueError('Option quote cannot be from the future')
        return self

CONTEXT_KEYS = ['fii_cash','dii_cash','fii_derivatives','us_markets','asia_markets',
                'gift_nifty','usd_inr','dollar_index','us_yield','crude','gold','copper',
                'india_vix','breadth','events']

class ContextRecord(Model):
    key: str
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=200)
    observed_at: datetime
    published_at: datetime
    valid_until: datetime
    note: str = Field(default='', max_length=500)
    @field_validator('key')
    @classmethod
    def valid_key(cls, value):
        if value not in CONTEXT_KEYS:
            raise ValueError('Unsupported context key')
        return value
    _aware = field_validator('observed_at','published_at','valid_until')(aware)
    @model_validator(mode='after')
    def timing(self):
        if self.published_at < self.observed_at or self.valid_until < self.published_at:
            raise ValueError('Require observed_at <= published_at <= valid_until')
        return self

class Dataset(Model):
    name: str = Field(min_length=1, max_length=100)
    provenance: str = Field(min_length=3, max_length=400)
    candles: list[Candle] = Field(min_length=1, max_length=30000)
    snapshots: list[Snapshot] = Field(min_length=1, max_length=5000)
    @model_validator(mode='after')
    def unique(self):
        for rows in (self.candles,self.snapshots):
            ts=[r.timestamp for r in rows]
            if len(set(ts)) != len(ts):
                raise ValueError('Duplicate observation timestamps')
        return self

class Experiment(Model):
    dataset_id: int = Field(gt=0)
    strategy: Literal['structure','ema','opening_range'] = 'structure'
    route: Literal['chart','options','combined','trend_combined'] = 'combined'
    risk_per_trade: FiniteFloat = Field(gt=0, le=1000000)
    capital: FiniteFloat = Field(gt=0, le=100000000)
    stop_pct: FiniteFloat = Field(gt=0, lt=100)
    reward_multiple: FiniteFloat = Field(gt=0, le=10)
    costs_per_lot: FiniteFloat = Field(ge=0, le=100000)
    slippage_per_unit: FiniteFloat = Field(ge=0, le=10000)
    max_hold_minutes: int = Field(ge=1, le=180)
    max_spread_pct: FiniteFloat = Field(gt=0, le=20)
    @model_validator(mode='after')
    def risk(self):
        if self.risk_per_trade > self.capital:
            raise ValueError('Risk budget cannot exceed capital')
        return self

class PlanInput(Model):
    dataset_id: int | None = Field(default=None, gt=0)
    security_id: str = Field(min_length=1, max_length=60)
    capital: FiniteFloat = Field(gt=0, le=100000000)
    risk_budget: FiniteFloat = Field(gt=0, le=1000000)
    stop_quote: FiniteFloat = Field(gt=0)
    costs_per_lot: FiniteFloat = Field(ge=0, le=100000)
    slippage_per_unit: FiniteFloat = Field(ge=0, le=10000)
    max_spread_pct: FiniteFloat = Field(gt=0, le=20)

class JournalInput(Model):
    plan_id: str = Field(min_length=1, max_length=80)
    note: str = Field(default='', max_length=1000)
