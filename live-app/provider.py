"""Read-only DhanHQ v2 adapter. Credentials stay on the server.

Endpoints verified against dhan hq v2 documentation. No broker execution API.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
import time
import httpx
from credentials import DhanCredentials,CredentialError

class ProviderError(Exception):pass

class Dhan:
    def __init__(self,credentials=None):
        self.lock=asyncio.Lock();self.last=0.
        self.credentials=credentials or DhanCredentials()
    @property
    def configured(self):
        return self.credentials.configured

    async def post(self,path,body):
        if not self.configured:raise ProviderError('Dhan credentials are not configured on the server')
        async with self.lock:
            wait=3.1-(time.monotonic()-self.last)
            if wait>0:await asyncio.sleep(wait)
            self.last=time.monotonic()
            try:
                token=self.credentials.token()
                async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
                    r=await client.post('https://api.dhan.co/v2/'+path,json=body,
                        headers={'access-token':token,'client-id':self.credentials.client_id})
                if r.status_code!=200:raise ProviderError(f'Dhan returned HTTP {r.status_code}; check credentials, entitlement and rate limits')
                data=r.json()
                if data.get('status')=='failure':raise ProviderError('Dhan rejected the data request')
                return data
            except (httpx.HTTPError,ValueError,CredentialError) as exc:
                raise ProviderError('Dhan connection or response failed; no substitute prices were generated') from None

