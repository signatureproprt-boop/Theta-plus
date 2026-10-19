from datetime import date,datetime,timezone
import asyncio
from market_board import parse_master
from session_chart import refresh_session
from store import Store

def test_real_vendor_future_names_resolve_exact_underlying_and_front_expiry():
    source='''EXCH_ID,SEGMENT,SECURITY_ID,SYMBOL_NAME,UNDERLYING_SYMBOL,INSTRUMENT,SM_EXPIRY_DATE
NSE,D,48704,NIFTY-Oct2026-FUT,NIFTY,FUTIDX,2026-10-27
NSE,D,61471,NIFTY-Nov2026-FUT,NIFTY,FUTIDX,2026-11-23
NSE,D,48699,BANKNIFTY-Oct2026-FUT,BANKNIFTY,FUTIDX,2026-10-27
NSE,D,111,NIFTY-Sep2026-FUT,NIFTY,FUTIDX,2026-09-29
NSE,D,222,NIFTYNXT50-Oct2026-FUT,NIFTYNXT50,FUTIDX,2026-10-27
NSE,C,333,NIFTY-Oct2026-FUT,NIFTY,FUTIDX,2026-10-20
BSE,D,444,NIFTY-Oct2026-FUT,NIFTY,FUTIDX,2026-10-20
'''
    items,_=parse_master(source,date(2026,10,19))
    assert items['nifty_future']['security_id']=='48704'
    assert items['banknifty_future']['security_id']=='48699'
    assert items['nifty_future']['segment']=='NSE_FNO'
    assert items['nifty_future']['expiry']=='2026-10-27'

def test_request_boundary_buffer_recovers_opening_bar_without_inventing_ohlc(tmp_path):
    clock=datetime(2026,10,19,3,48,tzinfo=timezone.utc)
    class Board:
        instruments={'nifty':{'security_id':'13','segment':'IDX_I','instrument':'INDEX'}}
        async def catalog(self):pass
    class Provider:
        async def post(self,endpoint,body):
            # Emulate a provider that excludes a bar exactly at fromDate.
            lower=int(datetime.fromisoformat(body['fromDate']+'+05:30').timestamp())
            upper=int(datetime.fromisoformat(body['toDate']+'+05:30').timestamp())
            stamps=[int(clock.replace(minute=45).timestamp())+i*60 for i in range(4)]
            stamps=[t for t in stamps if lower<t<upper]
            return {'timestamp':stamps,**{k:[100]*len(stamps) for k in ('open','high','low','close','volume')}}
    r=asyncio.run(refresh_session(Provider(),Store(tmp_path/'s.db'),Board(),'nifty',clock))
    assert len(r['candles'])==3 and r['missing_minutes']==0
    assert r['candles'][0]['timestamp']==int(clock.replace(minute=45).timestamp())
