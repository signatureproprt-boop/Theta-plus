"""Official DhanHQ-py data adapter. No broker execution methods are exposed."""
import asyncio
import copy
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

class ProviderError(Exception):
    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.code=code
        self.status=status

class DataSession(requests.Session):
    """Bound SDK requests, block redirects and sanitize errors before SDK logging."""
    def __init__(self, timeout):
        super().__init__()
        self.read_timeout=timeout
        self.status=None
        self.failure=None

    def request(self, method, url, **kwargs):
        if method.upper()!='POST' or url not in {'https://api.dhan.co/v2/'+p for p in DATA_PATHS}:
            self.failure='ENDPOINT';raise RuntimeError('Only approved Dhan data endpoints are enabled')
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
    def __init__(self,credentials=None):
        self.lock=asyncio.Lock();self.last=0.
        self.credentials=credentials or DhanCredentials()

    @property
    def configured(self):return self.credentials.configured

    @staticmethod
    def capabilities():
        return {'library':'dhanhq','version':SDK_VERSION,'rest':sorted(DATA_PATHS),
                'feeds':['live','depth20','depth200'],'execution':'NO_BROKER_EXECUTION'}

    def _request(self,path,body):
        token=self.credentials.token();client_id=self.credentials.client_id
        context=DhanContext(client_id,token)
        http=context.get_dhan_http()
        http.session.close()
        session=DataSession(60 if path.startswith('charts/') else 20)
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

    async def post(self,path,body):
        if path not in DATA_PATHS:raise ProviderError('Only Dhan market-data methods are enabled',code='ENDPOINT')
        if not self.configured:raise ProviderError('Dhan credentials are not configured on the server',code='DH-901',status=401)
        async with self.lock:
            wait=3.1-(time.monotonic()-self.last)
            if wait>0:await asyncio.sleep(wait)
            self.last=time.monotonic()
            try:return await asyncio.to_thread(self._request,path,body)
            except CredentialError:raise ProviderError('Dhan credentials unavailable',code='DH-901',status=401) from None
            except (KeyError,TypeError,ValueError):raise ProviderError('Invalid Dhan data request or response') from None
