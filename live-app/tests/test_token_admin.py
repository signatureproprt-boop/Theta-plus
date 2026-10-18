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
