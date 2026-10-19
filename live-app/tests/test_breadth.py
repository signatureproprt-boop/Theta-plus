import asyncio
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest
import breadth
from store import Store

CLOCK=datetime(2026,10,19,8,0,tzinfo=timezone.utc)
ISINS=['IN'+str(i).zfill(10) for i in range(1,51)]
CSV='Company Name,Industry,Symbol,Series,ISIN Code\n'+''.join(f'Stock{i},Bank,STOCK{i},EQ,{isin}\n' for i,isin in enumerate(ISINS,1))

def test_membership_rejects_duplicates_html_and_non_equity():
    assert len(breadth.parse_members(CSV))==50
    for text in [CSV+CSV.splitlines()[-1]+'\n','<html>Error</html>',CSV.replace(',EQ,',',BE,',1)]:
        with pytest.raises(ValueError):breadth.parse_members(text)

def record():
    return {'received_at':CLOCK.isoformat(),'membership_received_at':CLOCK.isoformat(),
            'rows':[{'trade_at':CLOCK.isoformat(),'direction':d} for d in ['up','up','up','down','flat']]}

def test_breadth_counts_all_members_and_flats_are_not_bearish():
    value=breadth.view(record(),CLOCK)
    assert value['fraction_up']==.6 and value['fraction_down']==.2
    assert (value['advances'],value['declines'],value['unchanged'])==(3,1,1)

@pytest.mark.parametrize('kind',['missing','stale','future','membership','received'])
def test_one_unknown_stale_or_future_member_hides_participation(kind):
    data=record()
    if kind=='missing':data['rows'][0]['direction']=None
    if kind in ('stale','future'):data['rows'][0]['trade_at']=(CLOCK+timedelta(seconds=-61 if kind=='stale' else 1)).isoformat()
    if kind=='membership':data['membership_received_at']=(CLOCK-timedelta(days=1)).isoformat()
    if kind=='received':data['received_at']=(CLOCK+timedelta(seconds=1)).isoformat()
    value=breadth.view(data,CLOCK)
    assert value['fraction_up'] is None and value['fraction_down'] is None


def test_exact_equity_isin_series_and_exchange_only():
    header='EXCH_ID,SEGMENT,INSTRUMENT,SERIES,ISIN,SECURITY_ID,SYMBOL_NAME\n'
    raw=header+f'NSE,E,EQUITY,EQ,{ISINS[0]},1,STOCK ONE LIMITED\n'+f'BSE,E,EQUITY,EQ,{ISINS[0]},2,STOCK1\n'+f'NSE,E,EQUITY,BE,{ISINS[0]},3,STOCK1\n'
    assert breadth.parse_equities(raw)=={ISINS[0]:[{'security_id':'1','master_name':'STOCK ONE LIMITED'}]}


def test_refresh_resolves_full_basket_and_stores_source_provenance(tmp_path,monkeypatch):
    class Fixed(datetime):
        @classmethod
        def now(cls,tz=None):return CLOCK
    monkeypatch.setattr(breadth,'datetime',Fixed)
    async def download(url):return CSV
    monkeypatch.setattr(breadth,'download_members',download)
    async def catalog():pass
    board=SimpleNamespace(catalog=catalog,equities={isin:[{'security_id':str(i),'master_name':f'Stock {i} Limited'}] for i,isin in enumerate(ISINS,1)})
    calls=[]
    class Provider:
        async def post(self,path,body):
            calls.append((path,body));return {'NSE_EQ':{str(i):{'last_price':101,'ohlc':{'close':100},'last_trade_time':'19/10/2026 13:30:00'} for i in range(1,51)}}
    store=Store(tmp_path/'breadth.db')
    value=asyncio.run(breadth.refresh(Provider(),store,board,'nifty',CLOCK))
    assert value['fraction_up']==1 and len(calls)==1
    assert calls[0]==('marketfeed/quote',{'NSE_EQ':list(range(1,51))})
    assert value['membership_source']==breadth.SOURCES['nifty']
    assert store.document('research/breadth/nifty')[0]['raw']
    asyncio.run(breadth.refresh(Provider(),store,board,'nifty',CLOCK));assert len(calls)==1
    board.equities[ISINS[0]].append({'security_id':'999','master_name':'Stock 1 Limited'})
    # Expire the quote cache; an ambiguous master must not become a smaller basket.
    store.put_document('research/breadth/nifty',None,store.document('research/breadth/nifty')[1])
    value=asyncio.run(breadth.refresh(Provider(),store,board,'nifty',CLOCK))
    assert value['state']=='MEMBERSHIP_OR_IDENTITY_UNAVAILABLE' and len(calls)==1


def test_outside_session_never_fetches_or_replays_stale_breadth(tmp_path):
    class Provider:
        async def post(self,*args):raise AssertionError('No after-hours fetch')
    value=asyncio.run(breadth.refresh(Provider(),Store(tmp_path/'off.db'),None,'nifty',CLOCK.replace(hour=16)))
    assert value['state']=='OUTSIDE_REGULAR_SESSION' and value['fraction_up'] is None


def test_persisted_recent_breadth_is_not_used_outside_session():
    data=record();off=CLOCK.replace(hour=16)
    data['received_at']=off.isoformat()
    for row in data['rows']:row['trade_at']=off.isoformat()
    value=breadth.view(data,off)
    assert value['state']=='OUTSIDE_REGULAR_SESSION' and value['fraction_up'] is None
