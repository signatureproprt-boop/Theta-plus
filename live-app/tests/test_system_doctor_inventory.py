import threading
from types import SimpleNamespace

import system_doctor as doctor
from collection import replace
from store import Store


def test_reads_are_parallel_bounded_and_return_in_cursor_order():
    barrier=threading.Barrier(8)
    lock=threading.Lock()
    active=0
    maximum=0

    def read(namespace,key):
        nonlocal active,maximum
        with lock:
            active+=1
            maximum=max(maximum,active)
        if int(key)<8:barrier.wait(timeout=3)
        with lock:active-=1
        return {'key':key}

    names=[str(i) for i in range(17)]
    rows=list(doctor.inventory_reads(SimpleNamespace(diagnostic_read=read),'records',names,float('inf')))
    assert [key for key,_,_ in rows]==names
    assert all(data=={'key':key} and error is None for key,data,error in rows)
    assert maximum==8


def test_deadline_finishes_inflight_batch_without_skipping_next_keys(monkeypatch):
    clock=[0]
    monkeypatch.setattr(doctor.time,'monotonic',lambda:clock[0])
    reads=[]
    def read(namespace,key):
        reads.append(key)
        if key=='3':raise ValueError('unreadable object')
        return key
    iterator=doctor.inventory_reads(SimpleNamespace(diagnostic_read=read),'records',list(map(str,range(20))),12)
    first=next(iterator)
    clock[0]=13
    rows=[first,*iterator]
    assert [row[0] for row in rows]==list(map(str,range(8)))
    assert sorted(reads)==list(map(str,range(8)))
    assert isinstance(rows[3][2],ValueError)
    assert rows[4][1]=='4'


def test_doctor_internal_documents_are_not_read():
    reads=[]
    def read(namespace,key):
        reads.append(key)
        return {}
    rows=list(doctor.inventory_reads(SimpleNamespace(diagnostic_read=read),'documents',
                                    ['doctor/current','jobs/one'],float('inf')))
    assert reads==['jobs/one']
    assert rows[0]==('doctor/current',None,None)


def test_tick_checkpoints_full_page_and_preserves_unreadable_evidence(tmp_path,monkeypatch):
    store=Store(tmp_path/'doctor.db')
    for i in range(103):store.put_document(f'archive/{i:03d}',{'record':i},0)
    scan=doctor.start(store)
    replace(store,doctor.KEY,lambda old:{**old,'state':'SCANNING','phase':'inventory'})
    original=store.diagnostic_read
    def read(namespace,key):
        if key=='archive/050':raise ValueError('one corrupt record')
        return original(namespace,key)
    monkeypatch.setattr(store,'diagnostic_read',read)
    collector=SimpleNamespace(store=store)
    doctor.tick(collector)
    state=store.document(doctor.KEY)[0]
    assert state['cursor']=='archive/099'
    assert state['inspected']=={'documents':100}
    assert state['scan_errors']==1
    assert state['findings']['unreadable_records']==1
    assert state['findings']['namespace:archive']==99
    # The short example list can fill before this error; full finding pages
    # must still preserve its diagnostic and exact object identity.
    pages=doctor.finding_pages(store)['pages']
    assert any(r['key']=='archive/050' and 'diagnostic' in r for page in pages for r in page['items'])
    doctor.tick(collector)
    state=store.document(doctor.KEY)[0]
    assert state['namespace']==1 and state['cursor']==''
    assert state['inspected']=={'documents':103}
    assert state['scan_errors']==1
    assert state['findings']['namespace:archive']==102
    assert state['id']==scan['id']
