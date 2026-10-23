"""Official DhanHQ-py data adapter. No broker execution methods are exposed."""
import asyncio
import copy
import json
import re
import time
from importlib.metadata import version
import requests
from dhanhq import DhanContext, dhanhq
from credentials import DhanCredentials, CredentialError

SDK_VERSION = version('dhanhq')
DATA_PATHS = {'charts/historical', 'charts/intraday', 'charts/rollingoption',
              'optionchain', 'optionchain/expirylist', 'marketfeed/ltp',
              'marketfeed/ohlc', 'marketfeed/quote'}

def quota_family(path):
    if path.startswith('marketfeed/'):return 'quote',1.1
    if path.startswith('optionchain'):return 'chain',3.2
    return 'history',0.25 # Four starts/sec, below the documented five/sec data limit.

class ProviderError(Exception):
    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.code=code
        self.status=status

class DataSession(requests.Session):
    """Bound SDK requests, block redirects and sanitize errors before SDK logging."""
    def __init__(self, timeout, *, commodity_probe=False):
        super().__init__()
        self.commodity_probe=commodity_probe
        self.read_timeout=timeout
        self.status=None
        self.failure=None

    def request(self, method, url, **kwargs):
        if method.upper()!='POST' or url not in {'https://api.dhan.co/v2/'+p for p in DATA_PATHS}:
            self.failure='ENDPOINT';raise RuntimeError('Only approved Dhan data endpoints are enabled')
        if self.commodity_probe and url!='https://api.dhan.co/v2/marketfeed/quote':
            self.failure='ENDPOINT';raise RuntimeError('Only a bounded commodity quote probe is enabled')
        # SDK 2.3 injects dhanClientId into every body. Quote endpoints accept
        # a segment -> ID-list map; client identity belongs in the headers.
        # Keep using the official SDK, correcting only this wire contract.
        if url.rsplit('/',1)[-1] in ('quote','ohlc','ltp') and '/marketfeed/' in url:
            payload=json.loads(kwargs.get('data') or '{}')
            if isinstance(payload,dict):payload.pop('dhanClientId',None)
            segments={'IDX_I','NSE_EQ','BSE_EQ','NSE_FNO','BSE_FNO','NSE_CURRENCY','BSE_CURRENCY','MCX_COMM'}
            if self.commodity_probe:
                if (url!='https://api.dhan.co/v2/marketfeed/quote' or not isinstance(payload,dict) or
                        set(payload)!={'NSE_COMM'} or not isinstance(payload['NSE_COMM'],list) or
                        not 1<=len(payload['NSE_COMM'])<=2):
                    self.failure='PAYLOAD';raise RuntimeError('Invalid bounded commodity support probe')
                segments={'NSE_COMM'}
            if (not isinstance(payload,dict) or not payload or
                any(k not in segments or
                    not isinstance(v,list) or not v or any(isinstance(i,bool) or not isinstance(i,int) or i<0 for i in v)
                    for k,v in payload.items()) or sum(map(len,payload.values()))>1000):
                self.failure='PAYLOAD';raise RuntimeError('Invalid market quote segment/ID payload')
            kwargs['data']=json.dumps(payload)
        kwargs.update(timeout=(10,self.read_timeout),allow_redirects=False)
        try:
            response=super().request(method,url,**kwargs)
        except requests.Timeout:
            self.failure='TIMEOUT';raise RuntimeError('Dhan data response timed out') from None
        except requests.RequestException:
            self.failure='NETWORK';raise RuntimeError('Dhan data connection failed') from None
        self.status=response.status_code
        return response


def sdk_request(client,path,body):
    """Translate the existing checkpointed request schema to public SDK methods."""
    if path in ('charts/historical','charts/intraday','charts/rollingoption'):
        common=dict(security_id=body['securityId'],exchange_segment=body['exchangeSegment'],
                    instrument_type=body['instrument'],from_date=body['fromDate'],to_date=body['toDate'])
        if path=='charts/historical':
            return client.historical_daily_data(**common,expiry_code=body.get('expiryCode',0),oi=body.get('oi',False))
        if path=='charts/intraday':
            return client.intraday_minute_data(**common,interval=int(body.get('interval',1)),oi=body.get('oi',False))
        return client.expired_options_data(**common,interval=int(body.get('interval',1)),
            expiry_flag=body['expiryFlag'],expiry_code=body['expiryCode'],strike=body['strike'],
            drv_option_type=body['drvOptionType'],required_data=body['requiredData'])
    if path=='optionchain':return client.option_chain(body['UnderlyingScrip'],body['UnderlyingSeg'],body['Expiry'])
    if path=='optionchain/expirylist':return client.expiry_list(body['UnderlyingScrip'],body['UnderlyingSeg'])
    if path=='marketfeed/quote':return client.quote_data(body)
    if path=='marketfeed/ohlc':return client.ohlc_data(body)
    if path=='marketfeed/ltp':return client.ticker_data(body)
    raise ProviderError('Only Dhan market-data methods are enabled',code='ENDPOINT')

class Dhan:
    def __init__(self,credentials=None,store=None):
        self.lock=asyncio.Lock();self.last=0.
        self.credentials=credentials or DhanCredentials()
        self.store=store

    def reserve(self,path):
        from collection import replace
        from datetime import datetime
        from runtime import IST
        reservation={};today=datetime.now(IST).date().isoformat();family,spacing=quota_family(path)
        def change(old):
            old=old or {};calls=old.get('calls',0) if old.get('day')==today else 0
            if calls>=100000:raise ProviderError('Daily market-data request budget reached',code='BUDGET',status=429)
            ts=time.time();families=dict(old.get('family_next_at') or {})
            global_at=old.get('global_next_at',0) if old.get('policy')=='endpoint-starts-v1' else old.get('next_at',0)
            at=max(ts,global_at,families.get(family,0),old.get('cooldown_until',0))
            reservation.update(granted=at<=ts,at=at)
            if at>ts:return old # Waiting/cancelled callers do not consume daily budget.
            families[family]=ts+spacing
            # Keep a conservative next_at for an older worker during rollout.
            # Modern callers use global_next_at plus family-specific spacing.
            return {**old,'day':today,'calls':calls+1,
                    'next_at':max(old.get('next_at',0),ts+spacing),'global_next_at':ts+0.25,
                    'family_next_at':families,'policy':'endpoint-starts-v1'}
        replace(self.store,'control/provider-budget',change)
        return reservation

    def rate_backoff(self):
        from collection import replace
        until=time.time()+60
        replace(self.store,'control/provider-budget',lambda old:{**(old or {}),
            'next_at':max((old or {}).get('next_at',0),until),
            'cooldown_until':max((old or {}).get('cooldown_until',0),until)})

    @property
    def configured(self):return self.credentials.configured

    @staticmethod
    def capabilities():
        return {'library':'dhanhq','version':SDK_VERSION,'rest':sorted(DATA_PATHS),
                'feeds':['live','depth20','depth200'],'execution':'NO_BROKER_EXECUTION'}

    def _request(self,path,body,commodity_probe=False):
        token=self.credentials.token();client_id=self.credentials.client_id
        context=DhanContext(client_id,token)
        http=context.get_dhan_http()
        http.session.close()
        session=DataSession(60 if path.startswith('charts/') else 20,commodity_probe=commodity_probe)
        http.session=session
        try:
            result=sdk_request(dhanhq(context),path,copy.deepcopy(body))
        finally:session.close()
        if session.failure=='TIMEOUT':
            raise ProviderError('Dhan response timed out. Retry a shorter date range; no data was substituted.',code='TIMEOUT',status=504)
        if session.failure:
            raise ProviderError('Dhan connection failed; no substitute prices were generated',code=session.failure,status=502)
        if not isinstance(result,dict):raise ProviderError('Dhan SDK returned an invalid response')
        data=result.get('data')
        if result.get('status')!='success':
            remarks=result.get('remarks') or {}
            if not isinstance(remarks,dict):remarks={'error_message':str(remarks)}
            code=str(remarks.get('error_code') or '')
            detail=self._redact(remarks.get('error_message',''),token,client_id)
            raise ProviderError(f'Dhan request rejected (HTTP {session.status or "unreported"}, code {code or "unreported"})'+(': '+detail if detail else ''),code=code,status=session.status)
        if not isinstance(data,dict) or not data:raise ProviderError('Dhan returned an invalid or empty response')
        if data.get('status')=='failure' or data.get('errorCode'):
            nested=data.get('data') if isinstance(data.get('data'),dict) else {}
            code=str(data.get('errorCode') or nested.get('code') or '')
            detail=self._redact(data.get('errorMessage') or nested.get('message') or '',token,client_id)
            raise ProviderError('Dhan request rejected'+(': '+detail if detail else ''),code=code,status=session.status)
        return data

    @staticmethod
    def _redact(detail,token,client_id):
        detail=str(detail)
        for value in (token,client_id):
            if value:detail=detail.replace(value,'[redacted]')
        detail=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[redacted]',detail)
        return ' '.join(detail.split())[:300]

    async def probe_commodity_quotes(self,security_ids):
        if (not isinstance(security_ids,list) or not 1<=len(security_ids)<=2 or
                any(isinstance(sid,bool) or not isinstance(sid,int) or sid<=0 for sid in security_ids)):
            raise ProviderError('Choose at most two pinned commodity identities',code='PAYLOAD')
        return await self.post('marketfeed/quote',{'NSE_COMM':security_ids},_commodity_probe=True)

    async def post(self,path,body,*,_commodity_probe=False):
        if path not in DATA_PATHS:raise ProviderError('Only Dhan market-data methods are enabled',code='ENDPOINT')
        if _commodity_probe and path!='marketfeed/quote':raise ProviderError('Invalid commodity support endpoint',code='ENDPOINT')
        if not await asyncio.to_thread(lambda:self.configured):raise ProviderError('Dhan credentials are not configured on the server',code='DH-901',status=401)
        async with self.lock:
            if self.store:
                # Recheck after every wait; do not pre-book future starts that
                # could bunch together when a delayed process wakes up.
                while True:
                    slot=await asyncio.to_thread(self.reserve,path)
                    if slot['granted']:break
                    await asyncio.sleep(max(0.01,slot['at']-time.time()))
            else:
                # Conservative fallback for callers without shared persistence.
                wait=3.1-(time.monotonic()-self.last)
                if wait>0:await asyncio.sleep(wait)
                self.last=time.monotonic()
            try:
                if _commodity_probe:return await asyncio.to_thread(self._request,path,body,commodity_probe=True)
                return await asyncio.to_thread(self._request,path,body)
            except ProviderError as error:
                if self.store and (error.status==429 or error.code in ('805','DH-904')):
                    await asyncio.to_thread(self.rate_backoff)
                raise
            except CredentialError:raise ProviderError('Dhan credentials unavailable',code='DH-901',status=401) from None
            except (KeyError,TypeError,ValueError):raise ProviderError('Invalid Dhan data request or response') from None
