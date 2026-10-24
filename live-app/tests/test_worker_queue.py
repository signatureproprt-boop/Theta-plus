import asyncio
from types import SimpleNamespace
from store import Store
import worker


def test_worker_advances_retained_jobs_without_full_inventory_reads_each_turn(tmp_path,monkeypatch):
    import gap_recovery,system_doctor
    clock=[0];scans=[];recoveries=[];requests=[]
    s=Store(tmp_path/'db');real_sleep=asyncio.sleep
    async def sleep(seconds):clock[0]+=seconds;await real_sleep(0)
    async def run(jid):requests.append(jid);return {'state':'running','items_assessed':len(requests)}
    def inventory(store):scans.append(clock[0]);return iter(['a'*32])
    monkeypatch.setattr(worker,'running_jobs',inventory)
    monkeypatch.setattr(gap_recovery,'tick',lambda _:recoveries.append(clock[0]) or False)
    monkeypatch.setattr(system_doctor,'tick',lambda _:False)
    monkeypatch.setattr(worker,'launch_worker',lambda _:None)
    monkeypatch.setattr(worker.asyncio,'sleep',sleep)
    asyncio.run(worker.run_worker(SimpleNamespace(store=s,run=run),max_seconds=4,clock=lambda:clock[0]))
    assert len(requests)==4
    assert scans==[0,4] # Initial discovery plus bounded-execution continuation.
    assert recoveries==[0]
    assert s.document('control/worker')[0]['state']=='checkpointed'


def test_cached_active_id_still_observes_fresh_manual_pause(tmp_path,monkeypatch):
    from collection import Collector,CollectionPlan
    from test_collection import seed,Provider
    import gap_recovery,system_doctor
    s=Store(tmp_path/'db');seed(s);c=Collector(Provider(),s)
    plan=c.create(CollectionPlan(start='2026-10-21',end='2026-10-22',families=['daily']))
    c.control(plan['id'],'resume');clock=[0];calls=[];real_sleep=asyncio.sleep
    async def call(owner,path,body):
        from test_collection import candles
        calls.append(body);return candles('2026-10-21')
    async def sleep(seconds):
        clock[0]+=seconds
        c.control(plan['id'],'pause')
        await real_sleep(0)
    monkeypatch.setattr(c,'call',call)
    monkeypatch.setattr(gap_recovery,'tick',lambda _:False)
    monkeypatch.setattr(system_doctor,'tick',lambda _:False)
    monkeypatch.setattr(worker,'launch_worker',lambda _:None)
    monkeypatch.setattr(worker.asyncio,'sleep',sleep)
    asyncio.run(worker.run_worker(c,max_seconds=4,clock=lambda:clock[0]))
    assert len(calls)==1 and c.get_job(plan['id'])['state']=='paused'
