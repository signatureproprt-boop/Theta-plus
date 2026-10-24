"""Dhan instrument discovery and a quote board independent of option signals."""
import asyncio
import csv
import io
import math
import time
from datetime import datetime, date, timezone
import httpx
from runtime import IST
from provider import ProviderError

MASTER_URL='https://images.dhan.co/api-data/api-scrip-master-detailed.csv'
# Exact master symbols, never guessed IDs or ETF substitutes for foreign indices.
WATCH=[
 ('nifty','NIFTY 50','India','NIFTY','INDEX','NSE'),
 ('banknifty','Bank Nifty','India','BANKNIFTY','INDEX','NSE'),
 ('nifty_future','NIFTY current future','India','NIFTY','FUTIDX','NSE'),
 ('banknifty_future','BANKNIFTY current future','India','BANKNIFTY','FUTIDX','NSE'),
 ('sensex','Sensex','India','SENSEX','INDEX','BSE'),
 ('india_vix','India VIX','India','INDIA VIX','INDEX','NSE'),
 ('gift_nifty','GIFT Nifty','India','GIFTNIFTY','INDEX','NSE'),
 ('gold','Gold','Commodities','GOLD','FUTCOM','MCX'),
 ('silver','Silver','Commodities','SILVER','FUTCOM','MCX'),
 ('crude','Crude oil','Commodities','CRUDEOIL','FUTCOM','MCX'),
 ('natural_gas','Natural gas','Commodities','NATURALGAS','FUTCOM','MCX'),
 ('copper','Copper','Commodities','COPPER','FUTCOM','MCX'),
 ('usd_inr','USD/INR','Currency','USDINR','FUTCUR','NSE'),
]
EXTERNAL=[('nikkei','Nikkei 225','Asia'),('hang_seng','Hang Seng','Asia'),
 ('shanghai','Shanghai Composite','Asia'),('kospi','KOSPI','Asia'),
 ('sp500','S&P 500','US'),('nasdaq','Nasdaq Composite','US'),
 ('dollar_index','Dollar index','Currency'),
 ('sp500_future','S&P 500 futures','Global Futures'),
 ('nasdaq_future','Nasdaq 100 futures','Global Futures'),
 ('dow_future','Dow futures','Global Futures'),
 ('nikkei_future','Nikkei 225 futures','Global Futures'),
 ('gold_usd','Gold futures · USD','USD Commodities'),
 ('silver_usd','Silver futures · USD','USD Commodities'),
 ('wti_usd','WTI Crude · USD','USD Commodities'),
 ('brent_usd','Brent Crude · USD','USD Commodities')]

def parse_master(text,today):
    reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    required={'EXCH_ID','SEGMENT','SECURITY_ID','SYMBOL_NAME','INSTRUMENT','SM_EXPIRY_DATE'}
    if not required.issubset(reader.fieldnames or []):raise ValueError('Unexpected instrument master columns')
    chosen={};options={}
    for row in reader:
        symbol=row['SYMBOL_NAME'].strip().upper();inst=row['INSTRUMENT'];exchange=row['EXCH_ID']
        try:
            sid=str(int(row['SECURITY_ID']))
            expiry=date.fromisoformat(row['SM_EXPIRY_DATE'][:10]) if inst!='INDEX' else None
        except (ValueError,TypeError):continue
        if expiry and expiry<today:continue
        if inst=='OPTIDX' and exchange=='NSE' and row.get('UNDERLYING_SYMBOL') in ('NIFTY','BANKNIFTY'):
            try:
                lot=float(row['LOT_SIZE']);strike=float(row['STRIKE_PRICE']);side=row['OPTION_TYPE']
                if not lot.is_integer() or not 0<lot<=100000 or not math.isfinite(strike) or strike<=0 or side not in ('CE','PE'):continue
                options[sid]={'security_id':sid,'expiry':expiry.isoformat(),'strike':strike,'side':side,'lot_size':int(lot),'underlying':row['UNDERLYING_SYMBOL'].lower()}
            except (ValueError,KeyError):pass
        for key,label,group,wanted,kind,exch in WATCH:
            # Derivative SYMBOL_NAME includes its expiry (NIFTY-Oct2026-FUT).
            # Resolve the exact underlying column, then choose front expiry;
            # never strip suffixes or use a prefix that could select mini/micro.
            base=(row.get('UNDERLYING_SYMBOL') or '').strip().upper() if kind=='FUTIDX' else symbol
            if not base:base=symbol # Older masters/fixtures may use the plain exact name.
            if (base,inst,exchange)!=(wanted,kind,exch):continue
            segment='IDX_I' if inst=='INDEX' and row['SEGMENT']=='I' else 'NSE_FNO' if inst=='FUTIDX' and exchange=='NSE' and row['SEGMENT']=='D' else 'MCX_COMM' if exchange=='MCX' and row['SEGMENT']=='M' else 'NSE_CURRENCY' if inst=='FUTCUR' and row['SEGMENT']=='C' else None
            if not segment:continue
            item={'key':key,'label':label,'group':group,'security_id':sid,'segment':segment,
                  'expiry':expiry.isoformat() if expiry else None,'instrument':inst,
                  'currency':None if inst=='INDEX' else 'INR',
                  'unit':'index points' if inst=='INDEX' else 'INR futures quote'}
            # Front expiry, exact standard-size product. Never a continuous spot series.
            if key not in chosen or (item['expiry'] or '',sid)<(chosen[key]['expiry'] or '',chosen[key]['security_id']):chosen[key]=item
    return chosen,options

def number(value):
    if isinstance(value,bool):return None
    try:
        val=float(value)
        return val if math.isfinite(val) else None
    except (TypeError,ValueError):return None

def quote_time(value,now):
    for fmt in ('%d/%m/%Y %H:%M:%S','%Y-%m-%d %H:%M:%S'):
        try:
            at=datetime.strptime(value,fmt).replace(tzinfo=IST).astimezone(timezone.utc)
            if 2020<=at.year and at<=now:return at
        except (ValueError,TypeError):pass
    return None

def quote_row(instrument,quote,now):
    if not isinstance(quote,dict):quote={}
    price=number(quote.get('last_price'));change=number(quote.get('net_change'))
    at=quote_time(quote.get('last_trade_time'),now)
    valid=price is not None and price>0
    previous=price-change if valid and change is not None else None
    return {**instrument,'source':'DhanHQ','price':price if valid else None,
        'change':change if valid else None,
        'change_pct':100*change/previous if previous and previous>0 and change is not None else None,
        'previous_close':previous if previous and previous>0 else None,
        'fetched_at':now.isoformat(),'quote_at':at.isoformat() if at else None,
        'status':'NO_QUOTE' if not valid else 'TIME_UNAVAILABLE' if not at else 'RECENT_TRADE' if (now-at).total_seconds()<=60 else 'STALE',
        'time_basis':'last trade time; not an exchange quote timestamp',
        'note':'Futures contract, not international spot' if instrument['expiry'] else 'Quote freshness requires a valid provider timestamp'}

class MarketBoard:
    def __init__(self,provider,store):
        self.provider=provider;self.store=store;self.lock=asyncio.Lock();self.catalog_lock=asyncio.Lock()
        self.instruments={};self.options={};self.equities={};self.master_at=0.;self.attempt_at=0.;self.master_day=None
        self.error=None;self.catalog_error=None

    async def catalog(self):
        async with self.catalog_lock:
            await self._catalog()

    async def _catalog(self):
        today=datetime.now(IST).date()
        if self.master_day==today and time.monotonic()-self.master_at<21600:return
        raw=bytearray()
        async with httpx.AsyncClient(timeout=60,follow_redirects=False) as client:
            async with client.stream('GET',MASTER_URL) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>100*1024*1024:raise ValueError('Instrument master exceeded size limit')
        instruments,options=await asyncio.to_thread(parse_master,raw.decode('utf-8-sig'),today)
        if not instruments:raise ValueError('No recognised instruments in master')
        from breadth import parse_equities
        self.equities=await asyncio.to_thread(parse_equities,raw.decode('utf-8-sig'))
        self.instruments,self.options=instruments,options
        self.master_at=time.monotonic();self.master_day=today;self.catalog_error=None

    async def refresh(self):
        if not await asyncio.to_thread(lambda:self.provider.configured):raise ProviderError('Dhan credentials are not configured')
        async with self.lock:
            if time.monotonic()-self.attempt_at<30:return await asyncio.to_thread(self.view)
            self.attempt_at=time.monotonic()
            try:await self.catalog()
            except Exception:
                self.catalog_error='Instrument master unavailable; catalog was not refreshed'
                if not self.instruments:
                    self.error=self.catalog_error
                    return await asyncio.to_thread(self.view)
            items=[];errors=[]
            # Isolate segments: an MCX entitlement failure must not hide index data.
            for segment in sorted({i['segment'] for i in self.instruments.values()}):
                instruments=[i for i in self.instruments.values() if i['segment']==segment and (not i['expiry'] or i['expiry']>=datetime.now(IST).date().isoformat())]
                if not instruments:continue
                try:
                    result=await self.provider.post('marketfeed/quote',{segment:[int(i['security_id']) for i in instruments]})
                    quotes=result.get('data',{}).get(segment,{})
                    if not isinstance(quotes,dict):raise ProviderError('Invalid quote response')
                    now=datetime.now(timezone.utc)
                    rows=[quote_row(i,quotes.get(i['security_id'],{}),now) for i in instruments]
                    # Some catalogued indices have no depth quote. One exact-ID
                    # ticker fallback may supply LTP, but never a quote timestamp.
                    gift=next((r for r in rows if r['key']=='gift_nifty' and r['price'] is None),None)
                    if gift:
                        gift['attempted_endpoints']=['marketfeed/quote','marketfeed/ltp']
                        try:
                            ticker=await self.provider.post('marketfeed/ltp',{segment:[int(gift['security_id'])]})
                            entry=ticker.get('data',{}).get(segment,{}).get(gift['security_id'],{})
                            value=number(entry.get('last_price')) if isinstance(entry,dict) else None
                            if value is not None and value>0:
                                gift.update(price=value,change=None,change_pct=None,quote_at=None,
                                            fetched_at=datetime.now(timezone.utc).isoformat(),
                                            status='TIME_UNAVAILABLE',quote_endpoint='marketfeed/ltp',
                                            note='Dhan ticker LTP; exchange quote/trade time unavailable. Not certified live.')
                            else:
                                gift['note']='Exact master ID resolved; Dhan quote and ticker returned no positive price.'
                        except (ProviderError,ValueError,TypeError,AttributeError):
                            gift['note']='Depth quote has no price; ticker fallback failed. Provider availability remains unverified.'
                    items.extend(rows)
                except (ProviderError,ValueError,TypeError):
                    errors.append(segment)
                    items.extend({**i,'price':None,'status':'FETCH_FAILED','source':'DhanHQ','note':'Check data entitlement, token and provider response'} for i in instruments)
            self.error=('Quote fetch failed: '+', '.join(errors)) if errors else None
            await asyncio.to_thread(self.store.add,'market_board',{'items':items,'catalog_at':datetime.now(timezone.utc).isoformat(),
                'error':self.error,'catalog_error':self.catalog_error})
            return await asyncio.to_thread(self.view)

    def view(self):
        batches=self.store.rows('market_board',1)
        saved=batches[0]['data'] if batches else {}
        by_key={r['key']:dict(r) for r in saved.get('items',[])}
        now=datetime.now(timezone.utc)
        for r in by_key.values():
            if r.get('quote_at') and (now-datetime.fromisoformat(r['quote_at'])).total_seconds()>60 and r.get('price') is not None:r['status']='STALE'
            if r.get('expiry') and r['expiry']<datetime.now(IST).date().isoformat():r['status']='EXPIRED'
        items=[by_key.get(k,{'key':k,'label':label,'group':group,'price':None,'source':'DhanHQ',
            'status':'NOT_IN_CATALOG' if saved else 'NOT_FETCHED','note':'No current matching instrument resolved' if saved else 'Refresh market toolbar to resolve instruments'}) for k,label,group,*_ in WATCH]
        items.extend({'key':k,'label':label,'group':group,'price':None,'source':None,
            'status':'SEPARATE_FEED_REQUIRED','currency':'USD' if group=='USD Commodities' else None,
            'unit':{'gold_usd':'USD / troy oz','silver_usd':'USD / troy oz','wti_usd':'USD / barrel','brent_usd':'USD / barrel'}.get(k,'Contract feed required'),
            'note':'Separate authorised feed and exact contract/expiry required; MCX INR quotes are not USD futures prices'} for k,label,group in EXTERNAL)
        if hasattr(self.store,'live_quotes'):
            try:
                quotes=self.store.live_quotes([i for i in items if i.get('segment') and i.get('security_id')])
                for item in items:
                    quote=quotes.get(item['key'])
                    if quote:item.update(quote,quote_at=quote.get('trade_at'),status='RECORDED_TRADE')
            except Exception:pass
        return {'items':items,'error':self.error or saved.get('error'),'catalog_error':self.catalog_error or saved.get('catalog_error'),
            'fetched_at':batches[0]['created_at'] if batches else None,'used_in_signal':False}
