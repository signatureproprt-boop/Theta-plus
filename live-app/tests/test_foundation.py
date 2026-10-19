import pytest
from fastapi.testclient import TestClient
import server
from runtime import VERSION


@pytest.fixture
def app(tmp_path,monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for name in ('K_SERVICE','THETA_GCS_BUCKET','THETA_DHAN_CREDENTIALS_SECRET','THETA_DHAN_TOKEN_SECRET','DHAN_ACCESS_TOKEN','DHAN_CLIENT_ID'):
        monkeypatch.delenv(name,raising=False)
    return server.create_app(tmp_path/'retained.sqlite3')


@pytest.mark.parametrize('path,method',[('overview','get'),('plans','post'),('experiments','post'),('refresh','post'),('datasets','post'),('journal','post')])
def test_retired_analysis_no_longer_runs(app,path,method):
    c=TestClient(app)
    response=getattr(c,method)('/api/'+path,**({'json':{}} if method=='post' else {}))
    assert response.status_code==410


def test_archive_retains_original_records_without_mutations(app):
    store=app.state.store
    original={'name':'original data','candles':[],'provenance':'user record'}
    saved_id=store.add('dataset',original)
    store.add('experiment',{'result':'unvalidated legacy result'})
    store.journal_add('old-plan',{'note':'retain journal'})
    before=(store.rows('dataset'),store.rows('experiment'),store.journal_rows())
    c=TestClient(app)
    assert c.get('/api/archive/dataset').json()[0]['data']==original
    assert c.get('/api/archive/journal').json()[0]['note']=='retain journal'
    c.post('/api/plans',json={});c.post('/api/experiments',json={})
    assert before==(store.rows('dataset'),store.rows('experiment'),store.journal_rows())
    assert store.get('dataset',saved_id)['data']==original


def test_saved_dhan_raw_and_quality_stay_accessible(app):
    payload={'metadata':{'kind':'index','index':'nifty','rows':0},'raw':{k:[] for k in ('timestamp','open','high','low','close','volume')}}
    saved=app.state.store.add('dhan_history',payload)
    c=TestClient(app)
    assert c.get(f'/api/dhan/collected/dhan_history/{saved}').json()==payload
    assert c.get('/api/dhan/collected').json()['dhan_history'][0]['id']==saved
    assert c.get(f'/api/dhan/collected/dhan_history/{saved}/verify').json()['status']=='EMPTY'


def test_foundation_ui_and_identity(app):
    c=TestClient(app);page=c.get('/').text
    assert 'DATA FOUNDATION' in page and 'page-admin' in page and 'page-saved' in page
    assert all(text not in page for text in ('page-room','Strategy lab','Trade planner','/static/room.js','/static/markets.js'))
    assert c.get('/health-check').json()['version']==VERSION
    status=c.get('/api/status').json()
    assert status['analysis_state']=='READ_ONLY_RESEARCH_UNVALIDATED' and status['saved_data']=='RETAINED'
    assert status['execution']=='NO_BROKER_EXECUTION'
    assert c.get('/api/archive/credential_status').status_code==404


def test_saved_history_can_load_independently_of_chains(app):
    s=app.state.store
    s.add('dhan_history',{'metadata':{'kind':'index','index':'nifty','rows':1},'raw':{}})
    s.add('dhan_chain',{'metadata':{'kind':'live_option_chain','index':'nifty'},'raw':{}})
    c=TestClient(app)
    assert set(c.get('/api/dhan/collected?kind=dhan_history').json())=={'dhan_history'}
    assert c.get('/api/dhan/collected?kind=secret').status_code==422
