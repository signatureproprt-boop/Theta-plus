import pytest
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
import admin_auth
import server
import token_check


def identity(email='owner@gmail.com', **changes):
    return dict(sub='google-user-1', email=email, email_verified=True, **changes)


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv('THETA_GOOGLE_CLIENT_ID', 'web-client')
    monkeypatch.setenv('THETA_ADMIN_EMAILS', 'owner@gmail.com')
    monkeypatch.setattr(admin_auth.id_token, 'verify_oauth2_token', lambda *a, **kw: identity())


def request():
    return Request({'type':'http', 'headers':[(b'x-theta-admin-id-token',b'google-id-token')]})


def test_admin_audience_is_enforced(admin, monkeypatch):
    seen = []
    def verify(token, transport, audience):
        seen.append((token,audience))
        return identity()
    monkeypatch.setattr(admin_auth.id_token, 'verify_oauth2_token', verify)
    assert admin_auth.require_admin(request()) == 'google-user-1'
    assert seen == [('google-id-token','web-client')]


@pytest.mark.parametrize('claims', [identity('other@gmail.com'), dict(identity(),email_verified=False), dict(identity(),sub='')])
def test_non_admin_rejected(admin, monkeypatch, claims):
    monkeypatch.setattr(admin_auth.id_token,'verify_oauth2_token',lambda *a, **kw: claims)
    with pytest.raises(HTTPException) as error: admin_auth.require_admin(request())
    assert error.value.status_code == 403


def test_invalid_google_signature_rejected(admin, monkeypatch):
    def fail(*a, **kw): raise ValueError('untrusted')
    monkeypatch.setattr(admin_auth.id_token,'verify_oauth2_token', fail)
    with pytest.raises(HTTPException) as error: admin_auth.require_admin(request())
    assert error.value.status_code == 403


@pytest.fixture
def app(tmp_path, monkeypatch, admin):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    monkeypatch.setenv('DHAN_CLIENT_ID','fixed-client')
    monkeypatch.setenv('THETA_DHAN_TOKEN_SECRET','projects/test/secrets/dhan')
    monkeypatch.delenv('K_SERVICE',raising=False)
    monkeypatch.delenv('THETA_GCS_BUCKET',raising=False)
    return server.create_app(tmp_path/'token.sqlite3')


def test_no_google_login_cannot_update_token(app):
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
    r=c.post('/api/dhan/token',json={'token':token},headers={'X-Theta-Admin-ID-Token':'google-id-token'})
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
    r=TestClient(app).post('/api/dhan/token',json={'token':'x'*30},headers={'X-Theta-Admin-ID-Token':'id-token'})
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
