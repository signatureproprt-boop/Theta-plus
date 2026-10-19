import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
import admin_auth
import server
import token_check


@pytest.fixture
def admin(monkeypatch):
    import hashlib
    salt='12'*32
    digest=hashlib.pbkdf2_hmac('sha256',b'fixture-admin-password',bytes.fromhex(salt),600000).hex()
    monkeypatch.setenv('THETA_TOKEN_ADMIN_HASH',f'pbkdf2_sha256$600000${salt}${digest}')


def request(password):
    return Request({'type':'http','headers':[(b'x-theta-admin-password',password.encode())]})


def test_admin_password_required(admin):
    assert admin_auth.require_admin(request('fixture-admin-password'))=='token-admin'
    for value in ('','wrong','google-id-token'):
        with pytest.raises(HTTPException) as error:admin_auth.require_admin(request(value))
        assert error.value.status_code==403


def test_malformed_hash_fails_closed(monkeypatch):
    monkeypatch.setenv('THETA_TOKEN_ADMIN_HASH','broken')
    with pytest.raises(HTTPException) as error:admin_auth.require_admin(request('fixture-admin-password'))
    assert error.value.status_code==403


@pytest.fixture
def app(tmp_path, monkeypatch, admin):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    monkeypatch.setenv('DHAN_CLIENT_ID','fixed-client')
    monkeypatch.setenv('THETA_DHAN_TOKEN_SECRET','projects/test/secrets/dhan')
    monkeypatch.delenv('K_SERVICE',raising=False)
    monkeypatch.delenv('THETA_GCS_BUCKET',raising=False)
    return server.create_app(tmp_path/'token.sqlite3')


def test_no_admin_password_cannot_update_token(app):
    c=TestClient(app)
    assert c.post('/api/dhan/token',json={'token':'x'*30}).status_code == 403
    assert c.get('/api/status').status_code == 200


def test_check_before_save_and_no_token_return(app, monkeypatch):
    events=[]
    async def check(token, client):
        events.append(('check',client))
        return dict(status='AUTHENTICATED',token_validity_reported='20/10/2026 09:30',data_plan='Active',market_data_verified=False)
    monkeypatch.setattr(server,'check_token',check)
    monkeypatch.setattr(server.DhanCredentials,'update_token',lambda self, token:events.append(('save',token)))
    c=TestClient(app)
    token='candidate-secret-'+'x'*30
    r=c.post('/api/dhan/token',json={'token':token},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert r.status_code == 200
    assert events == [('check','fixed-client'),('save',token)]
    assert token not in r.text
    status=c.get('/api/status')
    assert status.json()['credential_check']['status'] == 'AUTHENTICATED'
    assert token not in status.text


def test_bad_candidate_never_saved(app,monkeypatch):
    async def check(*args): raise HTTPException(422,'Rejected')
    monkeypatch.setattr(server,'check_token',check)
    saves=[]
    monkeypatch.setattr(server.DhanCredentials,'update_token',lambda *a:saves.append(a))
    r=TestClient(app).post('/api/dhan/token',json={'token':'x'*30},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert r.status_code==422 and not saves


@pytest.mark.parametrize('status,profile,expected',[(401,{},422),(200,{'dhanClientId':'different'},422),(429,{},502),(200,{'dhanClientId':'fixed','tokenValidity':'tomorrow','dataPlan':'Active'},200)])
def test_dhan_profile_check(monkeypatch,status,profile,expected):
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,headers):
            assert url=='https://api.dhan.co/v2/profile'
            assert headers['dhanClientId']=='fixed'
            return httpx.Response(status,json=profile)
    monkeypatch.setattr(token_check.httpx,'AsyncClient',Client)
    import asyncio
    if expected==200:
        result=asyncio.run(token_check.check_token('candidate','fixed'))
        assert result['status']=='AUTHENTICATED'
        assert result['market_data_verified'] is False
    else:
        with pytest.raises(HTTPException) as error:asyncio.run(token_check.check_token('candidate','fixed'))
        assert error.value.status_code==expected


def test_admin_unlock_never_returns_secret(app):
    c=TestClient(app)
    assert c.post('/api/admin/check',json={}).status_code==403
    r=c.post('/api/admin/check',json={},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert r.status_code==200 and r.json()=={'authorised':True}
    assert 'fixture-admin-password' not in r.text


def test_setup_retries_without_echoing_password(tmp_path, monkeypatch, capsys):
    import getpass, sys
    from pathlib import Path
    script=(Path(__file__).parents[1]/'deploy/configure-token-admin.sh').read_text()
    code=script.split("<<'PYHASH'\n",1)[1].split('\nPYHASH',1)[0]
    values=iter(['', 'short', 'é'*16, 'valid-password-123','different-password', 'chosen-password-123','chosen-password-123'])
    monkeypatch.setattr(getpass,'getpass',lambda prompt:next(values))
    target=tmp_path/'hash'
    monkeypatch.setattr(sys,'argv',['setup',str(target)])
    exec(compile(code,'setup','exec'),{})
    output=capsys.readouterr().out
    assert 'Received 0 characters' in output and 'English keyboard' in output and 'two entries differ' in output
    assert 'chosen-password-123' not in output and 'valid-password-123' not in output
    assert admin_auth.verify_password('chosen-password-123',target.read_text())


def test_setup_password_cancel_does_not_write(tmp_path,monkeypatch):
    import getpass,sys
    from pathlib import Path
    script=(Path(__file__).parents[1]/'deploy/configure-token-admin.sh').read_text()
    code=script.split("<<'PYHASH'\n",1)[1].split('\nPYHASH',1)[0]
    def cancel(prompt):raise EOFError()
    monkeypatch.setattr(getpass,'getpass',cancel)
    target=tmp_path/'hash'
    monkeypatch.setattr(sys,'argv',['setup',str(target)])
    with pytest.raises(SystemExit,match='cancelled'):exec(compile(code,'setup','exec'),{})
    assert not target.exists()


@pytest.mark.parametrize('path',['settings','test','save'])
def test_admin_connection_routes_require_password(app,path):
    assert TestClient(app).post('/api/admin/dhan/'+path,json={}).status_code==403


def test_connection_settings_never_return_saved_token(app,monkeypatch):
    monkeypatch.setenv('THETA_DHAN_CREDENTIALS_SECRET','projects/test/secrets/pair')
    monkeypatch.setattr(server.DhanCredentials,'pair',lambda self:{'client_id':'fixed-client','token':'never-return-this-secret'})
    c=TestClient(app)
    r=c.post('/api/admin/dhan/settings',json={},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert r.status_code==200 and r.json()['client_id']=='fixed-client'
    assert 'never-return-this-secret' not in r.text


def test_connection_test_does_not_save(app,monkeypatch):
    async def check(token,client):return {'status':'AUTHENTICATED','market_data_verified':False}
    monkeypatch.setattr(server,'check_token',check)
    saves=[]
    monkeypatch.setattr(server.DhanCredentials,'save_pair',lambda *a:saves.append(a))
    r=TestClient(app).post('/api/admin/dhan/test',json={'client_id':'new-client','token':'x'*30},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert r.status_code==200 and r.json()['saved'] is False and not saves


def test_connection_save_checks_then_saves_pair(app,monkeypatch):
    monkeypatch.setenv('THETA_DHAN_CREDENTIALS_SECRET','projects/test/secrets/pair')
    events=[]
    async def check(token,client):
        events.append(('check',client,token));return {'status':'AUTHENTICATED'}
    monkeypatch.setattr(server,'check_token',check)
    monkeypatch.setattr(server.DhanCredentials,'save_pair',lambda self,c,t:events.append(('save',c,t)))
    r=TestClient(app).post('/api/admin/dhan/save',json={'client_id':'new-client','token':'x'*30},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert r.status_code==200 and events==[('check','new-client','x'*30),('save','new-client','x'*30)]
    assert 'x'*30 not in r.text


def test_atomic_pair_cross_worker_and_failed_save(monkeypatch):
    from credentials import DhanCredentials,CredentialError
    import json
    from types import SimpleNamespace
    monkeypatch.setenv('THETA_DHAN_CREDENTIALS_SECRET','projects/test/secrets/pair')
    class Secret:
        data=json.dumps({'client_id':'old-client','token':'old-token-'+'x'*30}).encode()
        fail=False
        def access_secret_version(self,request):return SimpleNamespace(payload=SimpleNamespace(data=self.data))
        def add_secret_version(self,request):
            if self.fail:raise RuntimeError()
            self.data=request['payload']['data']
    secret=Secret();worker=DhanCredentials(secret_client=secret,cache_seconds=0)
    assert worker.client_id=='old-client'
    worker.save_pair('new-client','new-token-'+'x'*30)
    reader=DhanCredentials(secret_client=secret,cache_seconds=0)
    assert reader.client_id=='new-client' and reader.token()=='new-token-'+'x'*30
    secret.fail=True
    with pytest.raises(CredentialError):worker.save_pair('bad-client','bad-token-'+'x'*30)
    assert reader.client_id=='new-client'


def test_admin_page_exists_without_login(app):
    page=TestClient(app).get('/').text
    assert 'data-page="admin"' in page and 'id="page-admin"' in page


def test_setup_normalizes_secret_access_resource_path(tmp_path,monkeypatch):
    import json,sys,subprocess
    from pathlib import Path
    script=(Path(__file__).parents[1]/'deploy/configure-token-admin.sh').read_text()
    code=script.split("<<'PYPAIR'\n",1)[1].split('\nPYPAIR',1)[0]
    config=tmp_path/'service.json';dest=tmp_path/'pair.json'
    config.write_text(json.dumps({'spec':{'template':{'spec':{'containers':[{'env':[{'name':'DHAN_CLIENT_ID','valueFrom':{'secretKeyRef':{'name':'projects/other-project/secrets/client-id','key':'3'}}}]}]}}}}))
    calls=[]
    def access(args,**kwargs):
        calls.append(args)
        return 'client-value' if '--secret=client-id' in args else 'test-token'
    monkeypatch.setattr(subprocess,'check_output',access)
    monkeypatch.setattr(sys,'argv',['setup',str(config),str(dest),'theta-pluse','projects/theta-pluse/secrets/theta-dhan-access-token'])
    exec(compile(code,'setup','exec'),{})
    assert '--secret=client-id' in calls[0] and '--project=other-project' in calls[0]
    assert '--secret=theta-dhan-access-token' in calls[1] and '--project=theta-pluse' in calls[1]
    assert not any('--secret=projects/' in arg for args in calls for arg in args)
    assert json.loads(dest.read_text())=={'client_id':'client-value','token':'test-token'}


def test_worker_dispatch_stays_behind_existing_admin_authorization(app,monkeypatch):
    jid='a'*32
    app.state.store.put_document('jobs/'+jid,{'id':jid,'state':'paused'},0)
    calls=[]
    monkeypatch.setenv('THETA_COLLECTION_WORKER','projects/theta-pluse/locations/asia-south1/jobs/theta-plus-collector')
    monkeypatch.setattr(server,'launch_worker',lambda store:calls.append(True) or {'mode':'cloud_run_job','state':'starting'})
    c=TestClient(app)
    assert c.post('/api/collection/jobs/'+jid+'/resume',json={}).status_code==403
    assert not calls
    response=c.post('/api/collection/jobs/'+jid+'/resume',json={},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert response.status_code==200 and response.json()['runner']['mode']=='cloud_run_job' and calls==[True]
    assert c.get('/api/collection/engine').json()['sdk']['version']=='2.3.0'


def test_token_edit_reuses_permanent_client_id_when_not_resubmitted(app,monkeypatch):
    events=[]
    monkeypatch.setenv('THETA_DHAN_CREDENTIALS_SECRET','projects/test/secrets/pair')
    monkeypatch.setattr(server.DhanCredentials,'client_id',property(lambda self:'fixed-client'))
    async def check(token,client):
        events.append(('check',client));return {'status':'AUTHENTICATED'}
    monkeypatch.setattr(server,'check_token',check)
    monkeypatch.setattr(server.DhanCredentials,'save_pair',lambda self,client,token:events.append(('save',client)))
    response=TestClient(app).post('/api/admin/dhan/save',json={'token':'candidate-'+'x'*30},headers={'X-Theta-Admin-Password':'fixture-admin-password'})
    assert response.status_code==200 and response.json()['saved']
    assert events==[('check','fixed-client'),('save','fixed-client')]
    assert 'candidate-' not in response.text
