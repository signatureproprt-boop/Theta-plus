"""Dhan token rotation without exposing token to the browser after submission."""
import os
import time

class CredentialError(Exception):pass

class DhanCredentials:
    def __init__(self,secret_client=None,cache_seconds=20):
        self._client=secret_client
        self._cache_seconds=cache_seconds
        self._token=None
        self._until=0

    @property
    def client_id(self):return os.environ.get('DHAN_CLIENT_ID','').strip()

    @property
    def secret_name(self):return os.environ.get('THETA_DHAN_TOKEN_SECRET','').strip().rstrip('/')

    def _secret_client(self):
        if self._client is None:
            from google.cloud import secretmanager
            self._client=secretmanager.SecretManagerServiceClient()
        return self._client

    def token(self):
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
        return bool(self.client_id and (self.secret_name or os.environ.get('DHAN_ACCESS_TOKEN')))

    def update_token(self,token):
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
