"""Read-only DhanHQ v2 adapter. Credentials stay on the server.

Endpoints verified against dhan hq v2 documentation. No broker execution API.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
import time
import httpx
import re
from credentials import DhanCredentials,CredentialError

class ProviderError(Exception):
    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.code=code
        self.status=status

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
                timeout=60 if path.startswith('charts/') else 20
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout,connect=10),follow_redirects=False) as client:
                    r=await client.post('https://api.dhan.co/v2/'+path,json=body,
                        headers={'access-token':token,'client-id':self.credentials.client_id})
                try:data=r.json()
                except ValueError:data={}
                if not isinstance(data,dict):raise ProviderError('Dhan returned an invalid response')
                code=str(data.get('errorCode') or (data.get('data',{}).get('code') if isinstance(data.get('data'),dict) else '') or '')
                if r.status_code!=200 or data.get('status')=='failure' or data.get('errorCode'):
                    detail=data.get('errorMessage') or (data.get('data',{}).get('message') if isinstance(data.get('data'),dict) else '') or ''
                    detail=str(detail).replace(token,'[redacted]').replace(self.credentials.client_id,'[redacted]')
                    detail=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[redacted]',detail)
                    detail=' '.join(detail.split())[:300]
                    raise ProviderError(f'Dhan request rejected (HTTP {r.status_code}, code {code or "unreported"})'+(': '+detail if detail else ''),code=code,status=r.status_code)
                if not isinstance(data,dict) or not data:
                    raise ProviderError('Dhan returned an invalid or empty response')
                return data
            except httpx.TimeoutException:
                raise ProviderError('Dhan response timed out. Retry a shorter date range; no data was substituted.',code='TIMEOUT',status=504) from None
            except (httpx.HTTPError,ValueError,CredentialError) as exc:
                raise ProviderError('Dhan connection or response failed; no substitute prices were generated') from None
