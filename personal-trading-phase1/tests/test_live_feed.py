import asyncio
import json
import logging
import struct
from datetime import datetime, timezone

import pytest
from websockets.asyncio.server import serve

from app.live_feed import (DhanLiveClient, ProtocolError, ProviderDisconnect,
                           SessionStarted, WIRE_LOG, parse_frame)


def packet(code=4, segment=1, sid=1333):
    payloads = {
        2: struct.pack('<fI', 100.5, 1792801800),
        4: struct.pack('<fHIfIIIffff', 100.5, 3, 1792801800, 100., 1500,
                       10, 20, 98., 99., 105., 97.),
        5: struct.pack('<I', 1234),
        6: struct.pack('<fI', 99., 1200),
        7: b'', 50: struct.pack('<H', 807),
    }
    payload = payloads[code]
    return struct.pack('<BHBI', code, len(payload) + 8, segment, sid) + payload


def test_quote_binary_values_and_utc_not_minute_candles():
    event, = parse_frame(packet())
    assert event['security_id'] == '1333' and event['exchange_segment'] == 'NSE_EQ'
    assert event['ltp'] == 100.5 and event['cumulative_volume'] == 1500
    assert event['last_quantity'] == 3 and event['day_high'] == 105
    assert datetime.fromisoformat(event['trade_time']).tzinfo == timezone.utc
    assert 'volume' not in event and 'timestamp' not in event


def test_ticker_auxiliary_and_concatenated_packets():
    events = parse_frame(b''.join(packet(code) for code in (2, 5, 6, 7)))
    assert [e['kind'] for e in events] == ['tick', 'open_interest', 'previous_close', 'market_status']


@pytest.mark.parametrize('data', [b'', b'abc', packet()[:-1], packet(segment=5),
    struct.pack('<BHBI', 99, 8, 1, 1), struct.pack('<BHBI', 4, 8, 1, 1), 'not binary'])
def test_bad_packets_fail_explicitly(data):
    with pytest.raises(ProtocolError):
        parse_frame(data)


def test_disconnect_code_not_misclassified_as_dh907():
    with pytest.raises(ProviderDisconnect) as caught:
        parse_frame(packet(50))
    assert caught.value.code == 807


def test_subscription_batches_at_100():
    async def exercise():
        sent = []
        class Socket:
            async def send(self, message):
                sent.append(json.loads(message))
        client = DhanLiveClient(None, None)
        await client._send_subscriptions(Socket(), tuple(('NSE_EQ', str(i)) for i in range(201)))
        assert [b['InstrumentCount'] for b in sent] == [100, 100, 1]
        assert all(b['RequestCode'] == 17 for b in sent)
    asyncio.run(exercise())


def test_real_socket_reconnect_resubscribe_session_events_and_cancellation(monkeypatch, caplog):
    # Local provider stub tests actual RFC6455 frames, not just mocked connect().
    monkeypatch.setattr('app.live_feed.random.uniform', lambda a, b: 0.001)
    async def exercise():
        subscriptions, sessions, delivered = [], [], []
        done = asyncio.Event()
        async def handler(ws):
            subscriptions.append(json.loads(await ws.recv()))
            await ws.ping(b'health')  # library automatically answers provider ping
            await ws.send(packet())
            if len(subscriptions) == 1:
                await ws.close(code=1012)
            else:
                await ws.wait_closed()
        async def event(value):
            delivered.append(value)
            if len(delivered) == 2:
                done.set()
        async def connected(info):
            sessions.append(info)
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            from websockets.asyncio.client import connect
            def connector(uri, **kwargs):
                assert 'token=secret-test' in uri
                return connect(f'ws://127.0.0.1:{port}', **kwargs)
            client = DhanLiveClient(event, connected, connector=connector,
                credential_loader=lambda: ('client', 'secret-test'),
                watchlist_loader=lambda: (('NSE_EQ', '1333'),))
            task = asyncio.create_task(client.run())
            try:
                await asyncio.wait_for(done.wait(), timeout=5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert not client._running
        assert len(subscriptions) == 2 and subscriptions[0] == subscriptions[1]
        assert [s.generation for s in sessions] == [1, 2]
        assert sessions[0].disconnected_at is None
        assert sessions[1].disconnected_at is not None
        assert all(s.reconciliation_required for s in sessions)
        assert [e['generation'] for e in delivered] == [1, 2]
    with caplog.at_level(logging.INFO, logger='app.live_feed'):
        asyncio.run(exercise())
    assert 'secret-test' not in caplog.text
    assert 'LIVE_FIRST_TICK' in caplog.text and 'LIVE_STOPPED' in caplog.text
    assert not WIRE_LOG.propagate


def test_auth_block_waits_for_new_token(monkeypatch):
    monkeypatch.setattr('app.live_feed.random.uniform', lambda a, b: 0)
    async def exercise():
        calls, reads = [], []
        done = asyncio.Event()
        def creds():
            reads.append(1)
            if len(reads) > 3:
                return 'client', 'new-token'
            return 'client', 'old-token'
        class Socket:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def send(self, message):
                pass
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise ProviderDisconnect(807)
        def connector(uri, **kwargs):
            calls.append(uri)
            if len(calls) == 2:
                done.set()
            return Socket()
        async def ignore(_):
            pass
        client = DhanLiveClient(ignore, ignore, connector=connector, credential_loader=creds,
            watchlist_loader=lambda: (('NSE_EQ', '1333'),), refresh_seconds=.001)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.wait_for(done.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(calls) == 2
        assert 'old-token' in calls[0] and 'new-token' in calls[1]
        assert len(reads) > len(calls)
    asyncio.run(exercise())


def test_watchlist_filter_is_read_only_and_keeps_mcx_on_hold(monkeypatch):
    from types import SimpleNamespace as Row
    from app import live_feed
    rows = [Row(exchange_segment='MCX_COMM', security_id='10', instrument_type='FUTCOM', mapping_verified=True),
            Row(exchange_segment='NSE_EQ', security_id='1333', instrument_type='EQUITY', mapping_verified=True),
            Row(exchange_segment='NSE_EQ', security_id='44', instrument_type='EQUITY', mapping_verified=False)]
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query): return self
        def scalars(self): return self
        def all(self): return rows
    monkeypatch.setattr(live_feed, 'session', DB)
    assert live_feed.load_watchlist() == (('NSE_EQ', '1333'),)
    assert len(rows) == 3 and rows[0].mapping_verified
    rows[:] = [Row(exchange_segment='NSE_EQ', security_id=str(i + 1),
                  instrument_type='EQUITY', mapping_verified=True) for i in range(5001)]
    with pytest.raises(ValueError, match='5000'):
        live_feed.load_watchlist()


def test_watchlist_change_requests_new_session():
    from app.live_feed import WatchlistChanged, credential_revision
    async def exercise():
        client = DhanLiveClient(None, None, refresh_seconds=.001,
            credential_loader=lambda: ('c', 't'),
            watchlist_loader=lambda: (('BSE_EQ', '123'),))
        with pytest.raises(WatchlistChanged):
            await client._monitor((('NSE_EQ', '1333'),), credential_revision(('c', 't')))
    asyncio.run(exercise())
