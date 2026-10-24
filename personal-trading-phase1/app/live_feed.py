"""Step 2.1: one opt-in Dhan v2 Quote connection. No DB writes or REST calls.

Callbacks must enqueue work quickly; never run historical downloads in them.
Provider day OHLC and cumulative volume are NOT minute candles or tick volume.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlencode

from sqlalchemy import select, or_
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from app.config import read_secret, settings
from app.db import session
from app.models import Instrument, Watchlist, MasterIdentity
from app.policy import validate_identity

LOG = logging.getLogger('app.live_feed')
# This logger sees authenticated URLs in library DEBUG output. Use a private,
# non-propagating logger rather than passing credentials to application logs.
WIRE_LOG = logging.Logger('dhan_wire_private', level=logging.CRITICAL + 1)
WIRE_LOG.addHandler(logging.NullHandler())
WIRE_LOG.propagate = False
SEGMENTS = {0: 'IDX_I', 1: 'NSE_EQ', 2: 'NSE_FNO', 3: 'NSE_CURRENCY',
            4: 'BSE_EQ', 7: 'BSE_CURRENCY', 8: 'BSE_FNO'}
HEADER = struct.Struct('<BHBI')
SIZES = {2: 16, 4: 50, 5: 12, 6: 16, 7: 8, 50: 10}
Identity = tuple[str, str]


class ProtocolError(ValueError):
    pass


class ProviderDisconnect(Exception):
    def __init__(self, code: int):
        self.code = code
        super().__init__('PROVIDER_DISCONNECT')


class WatchlistChanged(Exception):
    pass


@dataclass(frozen=True)
class SessionStarted:
    generation: int
    connected_at: datetime
    disconnected_at: datetime | None
    instruments: tuple[Identity, ...]
    # True even on first process start: DB checkpoints must survive restarts.
    reconciliation_required: bool = True


def parse_frame(data: bytes) -> list[dict]:
    """Decode whole packets; handle concatenation, reject truncated/unknown data.

    Quote request 17 returns 4 plus auxiliary 5/6; ticker 2 also supported.
    Index/Full layouts aren't guessed. Unexpected codes surface as protocol errors.
    """
    if not isinstance(data, bytes) or not data:
        raise ProtocolError('EXPECTED_BINARY_PACKET')
    events, offset = [], 0
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            raise ProtocolError('TRUNCATED_HEADER')
        code, length, segment, security_id = HEADER.unpack_from(data, offset)
        if code not in SIZES or length != SIZES[code] or offset + length > len(data):
            raise ProtocolError('UNSUPPORTED_OR_MALFORMED_PACKET')
        payload = data[offset + 8:offset + length]
        offset += length
        if code == 50:
            raise ProviderDisconnect(struct.unpack('<H', payload)[0])
        if code == 7:
            events.append({'kind': 'market_status', 'response_code': code})
            continue
        if segment not in SEGMENTS:
            raise ProtocolError('UNSUPPORTED_SEGMENT')  # MCX remains HOLD.
        event = {'exchange_segment': SEGMENTS[segment], 'security_id': str(security_id),
                 'response_code': code}
        if code == 2:
            price, stamp = struct.unpack('<fI', payload)
            event.update(kind='tick', ltp=price, trade_epoch=stamp)
        elif code == 4:
            price, quantity, stamp, atp, volume, sell, buy, op, cl, hi, lo = struct.unpack(
                '<fHIfIIIffff', payload)
            event.update(kind='tick', ltp=price, trade_epoch=stamp, last_quantity=quantity,
                         average_price=atp, cumulative_volume=volume,
                         total_sell_quantity=sell, total_buy_quantity=buy,
                         day_open=op, day_close=cl, day_high=hi, day_low=lo)
        elif code == 5:
            event.update(kind='open_interest', open_interest=struct.unpack('<I', payload)[0])
        elif code == 6:
            price, oi = struct.unpack('<fI', payload)
            event.update(kind='previous_close', previous_close=price, previous_open_interest=oi)
        if any(isinstance(v, float) and (not math.isfinite(v) or v < 0)
               for v in event.values()):
            raise ProtocolError('INVALID_PRICE')
        # Zero LTT has no valid trade timestamp. Preserve it as metadata, not a candle.
        if 'trade_epoch' in event:
            stamp = event['trade_epoch']
            event['trade_time'] = (datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                                   if stamp else None)
        events.append(event)
    return events


def load_watchlist(max_instruments=5000) -> tuple[Identity, ...]:
    """Read only; never delete, remap or alter instruments on feed failures."""
    with session() as db:
        from app.dhan import IST
        today = datetime.now(timezone.utc).astimezone(IST).date()
        query = select(Instrument).join(Watchlist,
            (Instrument.exchange_segment == Watchlist.exchange_segment) &
            (Instrument.security_id == Watchlist.security_id)).outerjoin(MasterIdentity,
            (Instrument.exchange_segment == MasterIdentity.exchange_segment) &
            (Instrument.security_id == MasterIdentity.security_id))
        query = query.where(or_(MasterIdentity.security_id.is_(None),
            MasterIdentity.blocked.is_(False) & or_(MasterIdentity.expiry.is_(None), MasterIdentity.expiry >= today)))
        rows = db.execute(query).scalars().all()
        result = set()
        for instrument in rows:
            try:
                validate_identity(instrument.exchange_segment, instrument.instrument_type)
            except ValueError:
                continue  # Includes immutable commodity HOLD.
            if not instrument.mapping_verified:
                continue
            sid = instrument.security_id
            if not sid.isascii() or not sid.isdecimal() or not 0 < int(sid) <= 0xFFFFFFFF:
                raise ValueError('INVALID_WATCHLIST_SECURITY_ID')
            result.add((instrument.exchange_segment, str(int(sid))))
    if len(result) > max_instruments:
        raise ValueError('CONFIGURED_CONNECTION_CAPACITY_5000_EXCEEDED')
    return tuple(sorted(result))


def credentials() -> tuple[str, str]:
    config = settings()
    return read_secret(config.dhan_client_id_file), read_secret(config.dhan_access_token_file)


def credential_revision(value: tuple[str, str]) -> bytes:
    return hashlib.sha256(json.dumps(value).encode()).digest()


class DhanLiveClient:
    def __init__(self, on_event: Callable[[dict], Awaitable[None]],
                 on_session: Callable[[SessionStarted], Awaitable[None]], *,
                 watchlist_loader=load_watchlist, credential_loader=credentials,
                 connector=connect, refresh_seconds=30, stable_seconds=60):
        self.on_event, self.on_session = on_event, on_session
        self.load_watchlist, self.credentials, self.connect = watchlist_loader, credential_loader, connector
        self.refresh_seconds, self.stable_seconds = refresh_seconds, stable_seconds
        self.generation = 0
        self._running = False

    async def _send_subscriptions(self, ws, instruments):
        for start in range(0, len(instruments), 100):
            batch = instruments[start:start + 100]
            await ws.send(json.dumps({'RequestCode': 17, 'InstrumentCount': len(batch),
                'InstrumentList': [{'ExchangeSegment': seg, 'SecurityId': sid} for seg, sid in batch]}))
            await asyncio.sleep(0.05)
        LOG.info('LIVE_SUBSCRIPTIONS_SENT count=%d generation=%d', len(instruments), self.generation)
        # Sent is NOT a provider acknowledgement or proof that ticks are fresh.

    async def _receive(self, ws, instruments):
        allowed = set(instruments)
        first = True
        async for frame in ws:
            for event in parse_frame(frame):
                if event['kind'] != 'market_status' and (
                    event['exchange_segment'], event['security_id']) not in allowed:
                    raise ProtocolError('UNREQUESTED_IDENTITY')
                event['received_at'] = datetime.now(timezone.utc).isoformat()
                event['generation'] = self.generation
                await asyncio.wait_for(self.on_event(event), timeout=2)
                if first and event['kind'] == 'tick':
                    LOG.info('LIVE_FIRST_TICK generation=%d', self.generation)
                    first = False
        raise ConnectionError('REMOTE_CLOSED')

    async def _monitor(self, instruments, revision):
        while True:
            await asyncio.sleep(self.refresh_seconds)
            current = await asyncio.to_thread(self.load_watchlist)
            creds = await asyncio.to_thread(self.credentials)
            if current != instruments or credential_revision(creds) != revision:
                # Reconnect replaces the entire snapshot: removed IDs stop streaming;
                # a fresh session event also covers newly added instruments.
                raise WatchlistChanged()

    async def _session(self, ws, instruments, revision, disconnected_at):
        self.generation += 1
        await self._send_subscriptions(ws, instruments)
        # Step 2.2 plugs in a fast durable enqueue here, never a REST download.
        await asyncio.wait_for(self.on_session(SessionStarted(
            self.generation, datetime.now(timezone.utc), disconnected_at, instruments)), timeout=2)
        receiver = asyncio.create_task(self._receive(ws, instruments))
        monitor = asyncio.create_task(self._monitor(instruments, revision))
        try:
            done, _ = await asyncio.wait((receiver, monitor), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            receiver.cancel()
            monitor.cancel()
            await asyncio.gather(receiver, monitor, return_exceptions=True)

    async def run(self):
        """Cancel this coroutine to stop; context closes socket and child tasks.

        Exactly one process must own this client. Step 2.3 adds deployment ownership;
        do not start one per Uvicorn worker or alongside an occupied account slot.
        """
        if self._running:
            raise RuntimeError('CLIENT_ALREADY_RUNNING')
        self._running = True
        failures, disconnected_at, blocked_revision = 0, None, None
        try:
            while True:
                started = None
                try:
                    instruments = await asyncio.to_thread(self.load_watchlist)
                    creds = await asyncio.to_thread(self.credentials)
                    revision = credential_revision(creds)
                    if not instruments or not all(creds) or revision == blocked_revision:
                        LOG.info('LIVE_WAITING watchlist_count=%d credentials_or_capacity_blocked=%s',
                                 len(instruments), revision == blocked_revision or not all(creds))
                        await asyncio.sleep(self.refresh_seconds)
                        continue
                    uri = 'wss://api-feed.dhan.co?' + urlencode({
                        'version': 2, 'token': creds[1], 'clientId': creds[0], 'authType': 2})
                    async with self.connect(uri, open_timeout=15, close_timeout=5,
                            ping_interval=20, ping_timeout=20, compression=None,
                            max_size=1048576, max_queue=32, logger=WIRE_LOG) as ws:
                        started = time.monotonic()
                        LOG.info('LIVE_CONNECTED generation=%d', self.generation + 1)
                        await self._session(ws, instruments, revision, disconnected_at)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Exception strings/tracebacks may contain the authenticated URI.
                    code = exc.code if isinstance(exc, ProviderDisconnect) else None
                    status = exc.response.status_code if isinstance(exc, InvalidStatus) else None
                    if code in {805, 806, 807, 808, 809, 810} or status in {401, 403}:
                        # 805 must not create a reconnect war with an existing collector.
                        # After freeing capacity, explicitly restart this test client.
                        blocked_revision = revision
                    disconnected_at = datetime.now(timezone.utc)
                    if started is not None and time.monotonic() - started >= self.stable_seconds:
                        failures = 0
                    failures = min(failures + 1, 10)
                    ceiling = min(60, 2 ** failures)
                    delay = random.uniform(ceiling / 2, ceiling)
                    if code == 805 or status == 429:
                        delay = max(delay, 60)  # connection limit evidence, not DH-907
                    LOG.warning('LIVE_RETRY error_type=%s provider_code=%s http_status=%s delay=%.2f',
                                type(exc).__name__, code, status, delay)
                    await asyncio.sleep(delay)
        finally:
            self._running = False
            LOG.info('LIVE_STOPPED')


async def smoke():
    """Opt-in console smoke test: no persistence and no credential output."""
    count = 0

    async def event(_):
        nonlocal count
        count += 1
        if count == 1 or count % 100 == 0:
            LOG.info('LIVE_EVENTS_DELIVERED count=%d', count)

    async def connected(info):
        LOG.info('LIVE_SESSION generation=%d instruments=%d reconciliation=NOT_INSTALLED',
                 info.generation, len(info.instruments))

    await DhanLiveClient(event, connected).run()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Step 2.1 receive-only smoke test')
    parser.add_argument('--confirm-free-connection', action='store_true')
    args = parser.parse_args()
    if not args.confirm_free_connection:
        parser.error('Confirm one Dhan account connection slot is free before running.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    try:
        asyncio.run(smoke())
    except KeyboardInterrupt:
        pass
