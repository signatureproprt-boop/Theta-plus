"""Read-only DhanHQ v2 adapter. Credentials stay on the server.

Endpoints verified against dhan hq v2 documentation. No broker execution API.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
import time
import httpx
from models import Candle, Contract, Snapshot
from engine import IST
from credentials import DhanCredentials,CredentialError

class ProviderError(Exception):pass

class Dhan:
    def __init__(self,credentials=None):
        self.lock=asyncio.Lock();self.last=0.;self.candles=[];self.candles_at=0.
        self.prior_candles=[];self.prior_date=None;self.prior_retry_at=0.
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

    async def refresh(self,metadata):
        now=datetime.now(IST)
        expiries=(await self.post('optionchain/expirylist',{'UnderlyingScrip':13,'UnderlyingSeg':'IDX_I'})).get('data',[])
        future=sorted(e for e in expiries if e>=now.date().isoformat())
        if not future:raise ProviderError('No active NIFTY expiry returned')
        expiry=future[0]
        raw=(await self.post('optionchain',{'UnderlyingScrip':13,'UnderlyingSeg':'IDX_I','Expiry':expiry})).get('data',{})
        spot=raw.get('last_price');oc=raw.get('oc')
        if not spot or not isinstance(oc,dict):raise ProviderError('Dhan option chain is incomplete')
        # Read fresh market-depth snapshots for nearby contracts, including last-trade times.
        near=sorted(oc,key=lambda k:abs(float(k)-float(spot)))[:7]
        ids=[int(oc[k][side]['security_id']) for k in near for side in ('ce','pe') if oc[k].get(side,{}).get('security_id')]
        quotes=(await self.post('marketfeed/quote',{'NSE_FNO':ids})).get('data',{}).get('NSE_FNO',{}) if ids else {}
        at=datetime.now(timezone.utc);contracts=[]
        for k in near:
            for side in ('ce','pe'):
                c=oc[k].get(side)
                if not c or not c.get('security_id'):continue
                security=str(c['security_id']);q=quotes.get(security,{})
                trade_time=None
                for fmt in ('%d/%m/%Y %H:%M:%S','%Y-%m-%d %H:%M:%S'):
                    try:
                        t=datetime.strptime(q.get('last_trade_time',''),fmt).replace(tzinfo=IST)
                        if t.year>=2020 and t<=at:trade_time=t
                        break
                    except ValueError:pass
                depth=q.get('depth',{});buys=depth.get('buy',[]);sells=depth.get('sell',[])
                # Do not combine a last-trade freshness proxy with older chain prices.
                bid=buys[0].get('price',0) if buys else 0
                ask=sells[0].get('price',0) if sells else 0
                if not buys or not sells:trade_time=None
                meta=metadata.get(security,{})
                lot=meta.get('lot_size') if meta.get('expiry')==expiry and float(meta.get('strike',-1))==float(k) and meta.get('side')==side.upper() else None
                contracts.append(Contract(security_id=security,expiry=expiry,strike=float(k),side=side.upper(),
                    lot_size=lot,bid=bid,ask=ask,bid_qty=buys[0].get('quantity',0) if buys else 0,
                    ask_qty=sells[0].get('quantity',0) if sells else 0,oi=c.get('oi',0),volume=c.get('volume',0),
                    iv=c.get('implied_volatility'),delta=c.get('greeks',{}).get('delta'),quote_at=trade_time))
        today_start=now.replace(hour=9,minute=15,second=0,microsecond=0)
        async def fetch_bars(start,end):
            rawbars=await self.post('charts/intraday',{'securityId':'13','exchangeSegment':'IDX_I','instrument':'INDEX',
                'interval':'1','oi':False,'fromDate':start.strftime('%Y-%m-%d %H:%M:%S'),
                'toDate':end.strftime('%Y-%m-%d %H:%M:%S')})
            keys=('timestamp','open','high','low','close','volume')
            if not all(isinstance(rawbars.get(k),list) for k in keys):raise ProviderError('Intraday candle response is incomplete')
            if len({len(rawbars[k]) for k in keys})!=1:raise ProviderError('Candle arrays have inconsistent lengths')
            return [Candle(timestamp=datetime.fromtimestamp(t,timezone.utc),open=o,high=h,low=l,close=c,volume=v)
                    for t,o,h,l,c,v in zip(*(rawbars[k] for k in keys))]
        if self.prior_date!=now.date() and time.monotonic()>=self.prior_retry_at:
            try:
                history=await fetch_bars(today_start-timedelta(days=14),today_start)
                self.prior_candles=[c for c in history if c.timestamp<today_start]
                self.prior_date=now.date()
            except ProviderError:
                # Current-session analysis may proceed; the daily gate remains UNKNOWN.
                self.prior_candles=[];self.prior_date=None
                self.prior_retry_at=time.monotonic()+600
        if time.monotonic()-self.candles_at>=60 or not self.candles:
            current=await fetch_bars(today_start,now)
            self.candles=[c for c in current if c.timestamp>=today_start]
            self.candles_at=time.monotonic()
        merged={c.timestamp:c for c in self.prior_candles+self.candles}
        return Snapshot(timestamp=at,spot=spot,source='DhanHQ; quote freshness uses last-trade-time proxy',contracts=contracts),[merged[t] for t in sorted(merged)]
