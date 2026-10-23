import asyncio
from types import SimpleNamespace
from store import Store

def test_termination_drains_worker_and_releases_owned_lease(tmp_path,monkeypatch):
    import worker,data_system,system_doctor,gap_recovery
    store=Store(tmp_path/'s.db');handlers={}
    monkeypatch.setattr(data_system,'enabled',lambda _:True)
    async def supervise(*args):await asyncio.Event().wait()
    monkeypatch.setattr(data_system,'supervise',supervise)
    monkeypatch.setattr(system_doctor,'tick',lambda _:None)
    monkeypatch.setattr(gap_recovery,'tick',lambda _:False)
    monkeypatch.setattr(worker,'running_jobs',lambda _:iter(()))
    def dispatch(*args):raise AssertionError('Termination must not dispatch another execution')
    monkeypatch.setattr(worker,'launch_worker',dispatch)
    async def run():
        loop=asyncio.get_running_loop()
        monkeypatch.setattr(loop,'add_signal_handler',lambda sig,callback:handlers.update({sig:callback}))
        monkeypatch.setattr(loop,'remove_signal_handler',lambda sig:handlers.pop(sig))
        task=asyncio.create_task(worker.serve(SimpleNamespace(store=store)))
        for _ in range(100):
            if store.document('control/worker')[0]:break
            await asyncio.sleep(.01)
        assert store.document('control/worker')[0]['until']>0
        handlers[worker.signal.SIGTERM]()
        await asyncio.wait_for(task,2)
    asyncio.run(run())
    assert store.document('control/worker')[0]['until']==0 and not handlers
