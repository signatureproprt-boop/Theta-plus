import asyncio
import time

import pytest
from fastapi import HTTPException

from collection import Collector
from request_review import acquire_diagnostic
from store import Store


def test_diagnosis_gets_next_turn_without_stealing_or_advancing(tmp_path):
    store=Store(tmp_path/'s.db')
    collector=Collector(None,store)
    active=collector.acquire()
    store.put_document('jobs/unchanged',{'cursor':{'offset':42},'state':'running'},0)

    async def scenario():
        pending=asyncio.create_task(acquire_diagnostic(collector,wait_seconds=3))
        # Wait for durable reservation, rather than relying on task scheduling.
        for _ in range(200):
            if (store.document('control/diagnostic-slot')[0] or {}).get('until',0)>time.time():break
            await asyncio.sleep(.01)
        else:raise AssertionError('diagnostic did not reserve a turn')
        assert store.document('control/lease')[0]['owner']==active
        with pytest.raises(HTTPException) as blocked:collector.acquire()
        assert blocked.value.status_code==409
        collector.release(active)
        return await pending

    diagnostic=asyncio.run(scenario())
    assert diagnostic!=active
    assert store.document('control/lease')[0]['owner']==diagnostic
    assert store.document('control/diagnostic-slot')[0]['until']==0
    assert store.document('jobs/unchanged')[0]=={'cursor':{'offset':42},'state':'running'}
    with pytest.raises(HTTPException):collector.acquire()
    collector.release(diagnostic)
    collector.release(collector.acquire())


def test_busy_timeout_preserves_active_owner_and_clears_reservation(tmp_path):
    store=Store(tmp_path/'s.db');collector=Collector(None,store)
    owner=collector.acquire();before=store.document('control/lease')[0]
    with pytest.raises(HTTPException) as error:
        asyncio.run(acquire_diagnostic(collector,wait_seconds=0))
    assert error.value.status_code==409 and 'did not run' in error.value.detail
    assert store.document('control/lease')[0]==before
    assert store.document('control/diagnostic-slot')[0]['until']==0
    collector.release(owner)


def test_expired_reservation_does_not_block_collection(tmp_path):
    store=Store(tmp_path/'s.db');collector=Collector(None,store)
    store.put_document('control/diagnostic-slot',{'owner':'crashed','until':time.time()-1},0)
    collector.release(collector.acquire())
    collector.release(asyncio.run(acquire_diagnostic(collector)))


def test_cancelled_wait_releases_only_its_reservation(tmp_path):
    store=Store(tmp_path/'s.db');collector=Collector(None,store)
    owner=collector.acquire()
    async def scenario():
        pending=asyncio.create_task(acquire_diagnostic(collector))
        for _ in range(200):
            if store.document('control/diagnostic-slot')[0]:break
            await asyncio.sleep(.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):await pending
    asyncio.run(scenario())
    assert store.document('control/diagnostic-slot')[0]['until']==0
    assert store.document('control/lease')[0]['owner']==owner
    collector.release(owner)
