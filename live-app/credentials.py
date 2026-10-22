"""Dhan token rotation without exposing token to the browser after submission."""
import json
import os
import time

class CredentialError(Exception):pass

class DhanCredentials:
    def __init__(self,secret_client=None,cache_seconds=20):
        self._client=secret_client
        self._cache_seconds=cache_seconds
        self._token=None
        self._until=0
        self._pair=None
        self._pair_until=0

    def invalidate(self):
        self._until=0;self._pair_until=0

    @property
    def pair_secret(self):return os.environ.get('THETA_DHAN_CREDENTIALS_SECRET','').strip().rstrip('/')

    def pair(self):
        if self._pair and time.monotonic()<self._pair_until:return self._pair
        try:
            raw=self._secret_client().access_secret_version(request={'name':self.pair_secret+'/versions/latest'})
            pair=json.loads(raw.payload.data.decode())
            if not isinstance(pair.get('client_id'),str) or not isinstance(pair.get('token'),str):raise ValueError()
        except Exception:raise CredentialError('Could not read saved Dhan connection') from None
        self._pair=pair;self._pair_until=time.monotonic()+self._cache_seconds
        return pair

    @property
    def client_id(self):
        return self.pair()['client_id'] if self.pair_secret else os.environ.get('DHAN_CLIENT_ID','').strip()

    def save_pair(self,client_id,token):
        if not self.pair_secret:raise CredentialError('Admin connection storage needs one-time setup')
        if not client_id or not 20<=len(token)<=4096 or any(c.isspace() for c in token):raise CredentialError('Invalid Dhan connection input')
        pair={'client_id':client_id,'token':token}
        try:
            self._secret_client().add_secret_version(request={'parent':self.pair_secret,'payload':{'data':json.dumps(pair).encode()}})
        except Exception:raise CredentialError('Could not save Dhan connection; previous credentials remain active') from None
        self._pair=pair;self._pair_until=time.monotonic()+self._cache_seconds
        return True

    @property
    def secret_name(self):return os.environ.get('THETA_DHAN_TOKEN_SECRET','').strip().rstrip('/')

    def _secret_client(self):
        if self._client is None:
            from google.cloud import secretmanager
            self._client=secretmanager.SecretManagerServiceClient()
        return self._client

    def token(self):
        if self.pair_secret:return self.pair()['token']
        if not self.secret_name:return os.environ.get('DHAN_ACCESS_TOKEN','')
        if self._token and time.monotonic()<self._until:return self._token
        try:
            response=self._secret_client().access_secret_version(request={'name':self.secret_name+'/versions/latest'})
            token=response.payload.data.decode('utf-8')
        except Exception:
            raise CredentialError('Could not read the current Dhan token') from None
        if not token:raise CredentialError('Current Dhan token is empty')
        self._token=token;self._until=time.monotonic()+self._cache_seconds
        return token

    @property
    def configured(self):
        try:return bool(self.client_id and (self.pair_secret or self.secret_name or os.environ.get('DHAN_ACCESS_TOKEN')))
        except CredentialError:return False

    def update_token(self,token):
        if self.pair_secret:return self.save_pair(self.client_id,token)
        if not self.client_id:raise CredentialError('Dhan Client ID is not configured on the server')
        if not self.secret_name:raise CredentialError('Token screen requires Secret Manager setup')
        if not isinstance(token,str) or not 20<=len(token)<=4096 or any(ch.isspace() for ch in token):
            raise CredentialError('Enter one complete Dhan token without spaces')
        try:
            self._secret_client().add_secret_version(request={
                'parent':self.secret_name,'payload':{'data':token.encode('utf-8')}})
        except Exception:
            raise CredentialError('Could not save new Dhan token') from None
        self._token=token;self._until=time.monotonic()+self._cache_seconds
        return True
