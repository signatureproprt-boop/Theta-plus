import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from collection import Collector
from instrument_directory import InstrumentDirectory
from provider import ProviderError
from store import Store

SHA='a'*64

def item(key='index', **kw):
    return {'key':key,'security_id':'13','exchange':'NSE','raw_segment':'I','segment':'IDX_I',
            'instrument':'INDEX','symbol':'NIFTY','display':'Nifty','expiry':None,'reason':None,**kw}

def seed(collector, items=None, sha=SHA):
    entries=items if items is not None else [item()]
    pages=[]
    for n in range(0,len(entries),3):
        key=f'catalogue/{sha}/pages/{n}'
        collector.store.put_document(key,{'items':entries[n:n+3]},0)
        pages.append({'key':key,'group':'IDX_I-INDEX','count':len(entries[n:n+3])})
    cat={'sha256':sha,'fetched_at':datetime.now(timezone.utc).isoformat(),'rows':len(entries),'pages':pages}
    collector.store.put_document('catalogue/current',cat,collector.store.document('catalogue/current')[1])
    return cat

@pytest.fixture
def directory(tmp_path):
    collector=Collector(SimpleNamespace(),Store(tmp_path/'db.sqlite3'));seed(collector)
    return InstrumentDirectory(collector)

def run(coro):return asyncio.run(coro)

def test_whole_master_pages_filters_literals_and_review_reasons(directory):
    entries=[item(str(n),symbol=f'STOCK{n}',segment='BSE_EQ',instrument='EQUITY',expiry='1900-01-01') for n in range(55)]
    entries += [item('literal',symbol='ABC%_'),item('unknown',symbol='GOLD',segment=None,reason='No documented mapping: NSE_M'),
                item('expired',symbol='GOLD',instrument='FUTCOM',expiry='2000-01-01',segment='MCX_COMM')]
    seed(directory.collector,entries,'b'*64)
    data=directory.search(segment='BSE_EQ')
    assert data['matches']==55 and len(data['items'])==50 and data['next_page']==1
    assert len(directory.search(segment='BSE_EQ',page=1)['items'])==5
    assert directory.search('%_')['items'][0]['key']=='literal'
    assert directory.search('GOLD')['matches']==1
    assert directory.search('GOLD',active_only=False)['matches']==2
    assert directory.search(segment='UNMAPPED')['items'][0]['reason']
    for args in [('','NSE_M'),('x'*81,'')]:
        with pytest.raises(HTTPException):directory.search(*args)

def test_stale_selection_unresolved_and_expired_contracts_are_not_queried(directory):
    seed(directory.collector,[item(),item('bad',segment=None,reason='No documented mapping'),
         item('old',instrument='FUTIDX',expiry='2000-01-01')],'b'*64)
    for key,sha,current in [('index',SHA,False),('bad','b'*64,False),('old','b'*64,True)]:
        with pytest.raises(HTTPException) as e:directory.resolve(key,sha,current)
        assert e.value.status_code==409
    assert directory.resolve('old','b'*64)['expiry']=='2000-01-01'
    cat=directory.collector.catalogue();cat['fetched_at']='2000-01-01T00:00:00+00:00'
    directory.collector.store.put_document('catalogue/current',cat,directory.collector.store.document('catalogue/current')[1])
    with pytest.raises(HTTPException) as e:directory.resolve('index','b'*64,True)
    assert e.value.status_code==409

@pytest.mark.parametrize('segment', ['IDX_I','NSE_EQ','BSE_EQ','NSE_FNO','BSE_FNO','NSE_CURRENCY','BSE_CURRENCY','MCX_COMM'])
def test_quote_keeps_exact_segment_and_security_id_and_releases_lease(directory,segment):
    seed(directory.collector,[item('exact',segment=segment)],'b'*64)
    calls=[]
    async def call(owner,path,body):
        calls.append((path,body))
        return {'data':{segment:{'13':{'last_price':123,'last_trade_time':(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat(),'volume':44,'oi':66}}}}
    directory.collector.call=call
    data=run(directory.quote('exact','b'*64))
    assert calls==[('marketfeed/quote',{segment:[13]})]
    assert data['segment']==segment and data['observation']['volume']==44
    assert data['status']=='TIME_UNAVAILABLE'
    assert directory.collector.store.rows('instrument_quote')[0]['data']['raw']==data['raw']
    assert directory.collector.store.document('control/lease')[0]['until']==0

def test_quote_provider_rejection_and_malformed_response_do_not_invent_price(directory):
    async def fail(*args):raise ProviderError('Dhan rejected request',code='DH-907')
    directory.collector.call=fail
    with pytest.raises(ProviderError):run(directory.quote('index',SHA))
    assert directory.collector.store.document('control/lease')[0]['until']==0
    async def malformed(*args):return {'data':[]}
    directory.collector.call=malformed
    assert run(directory.quote('index',SHA))['status']=='NO_QUOTE'

def test_history_uses_selected_identity_validates_and_preserves_rejected_raw(directory):
    calls=[]
    stamp=int(datetime(2026,10,22,4,0,tzinfo=timezone.utc).timestamp())
    raw={'timestamp':[stamp],'open':[100],'high':[102],'low':[99],'close':[101],'volume':[20]}
    async def call(owner,path,body):calls.append((path,body));return raw
    directory.collector.call=call
    result=run(directory.history('index',SHA,date(2026,10,22),date(2026,10,23)))
    assert result['quality']['rows']==1 and result['request']['securityId']=='13'
    assert calls[0][0]=='charts/intraday' and calls[0][1]['interval']=='1'
    assert directory.collector.store.document('candles/'+result['quality']['series']+'/2026-10-22')[0]
    raw['high']=[10]
    with pytest.raises(ProviderError):run(directory.history('index',SHA,date(2026,10,22),date(2026,10,23)))
    assert len(directory.collector.store.rows('instrument_history_source'))==2
    assert directory.collector.store.document('control/lease')[0]['until']==0
    raw['high']=[102]
    run(directory.history('index',SHA,date(2026,10,22),date(2026,10,23),'daily'))
    assert calls[-1][0]=='charts/historical' and calls[-1][1]['expiryCode']==0 and 'interval' not in calls[-1][1]

@pytest.mark.parametrize('start,end,family,interval',[
    (date(2026,10,23),date(2026,10,23),'intraday',1),
    (date(2026,1,1),date(2026,10,23),'intraday',1),
    (date(2026,10,22),date(2026,10,23),'intraday',3),
    (date(2026,10,22),date(2099,1,1),'daily',1)])
def test_invalid_history_bounds_make_no_provider_request(directory,start,end,family,interval):
    with pytest.raises(HTTPException) as e:run(directory.history('index',SHA,start,end,family,interval))
    assert e.value.status_code==422
    assert directory.collector.store.document('control/lease')[0] is None

def test_routes_require_admin_before_provider_access(tmp_path,monkeypatch):
    import server
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    monkeypatch.delenv('K_SERVICE',raising=False);monkeypatch.delenv('THETA_GCS_BUCKET',raising=False)
    app=server.create_app(tmp_path/'app.sqlite3');seed(app.state.collector)
    def admin(request):
        if request.headers.get('x-theta-admin-password')!='test':raise HTTPException(403,'Locked')
    monkeypatch.setattr(server,'require_admin',admin)
    client=TestClient(app);body={'key':'index','catalogue_sha256':SHA}
    assert client.get('/api/instruments?q=NIFTY').json()['items'][0]['key']=='index'
    assert client.get('/api/instruments/overview').json()['catalogue_rows']==1
    assert client.get('/api/instruments/saved-history',params={**body,'start':'2026-10-21','end':'2026-10-23'}).json()['rows']==0
    for path,payload in [('quote',body),('history',{**body,'start':'2026-10-22','end':'2026-10-23'})]:
        assert client.post('/api/instruments/'+path,json=payload).status_code==403
    async def call(*args):return {'data':{'IDX_I':{'13':{'last_price':100}}}}
    app.state.collector.call=call
    assert client.post('/api/instruments/quote',json=body,headers={'x-theta-admin-password':'test'}).json()['price']==100
    assert client.post('/api/instruments/quote',json={**body,'segment':'NSE_EQ'},headers={'x-theta-admin-password':'test'}).status_code==422
    assert client.post('/api/instruments/quote',json=body,headers={'Origin':'https://hostile.example','x-theta-admin-password':'test'}).status_code==403

def test_recorded_data_matches_segment_not_just_security_id_and_exposes_receipt_age(directory):
    seed(directory.collector,[item('equity',segment='BSE_EQ')],'b'*64)
    store=directory.collector.store
    assert directory.recorded('equity','b'*64)['state']=='NOT_SUBSCRIBED'
    store.put_document('system/subscription-plan',{'revision':'new','scopes':{'live-3':[item('equity',segment='BSE_EQ')]}},0)
    assert directory.recorded('equity','b'*64)['state']=='AWAITING_OBSERVATION'
    at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    store.put_document('system/latest/live-3',{'rows':{'1:13':{'segment':'NSE_EQ','security_id':'13','received_at':at,'ltp':1},
         '4:13':{'segment':'BSE_EQ','security_id':'13','received_at':at,'ltp':200}}},0)
    result=directory.recorded('equity','b'*64)
    assert result['observation']['ltp']==200 and result['receipt_age_seconds']>=300
    assert result['state']=='RETAINED_OBSERVATION' and result['recorder']=='live-3'

def test_master_replacement_keeps_open_readers_valid_and_bounds_disk_indexes(directory):
    with directory.database() as (db,cat):
        seed(directory.collector,[item('new')],'b'*64)
        assert directory.search()['items'][0]['key']=='new'
        assert db.execute('SELECT key FROM instruments').fetchone()[0]=='index'
    assert len(list(__import__('pathlib').Path(directory.temporary.name).glob('*.sqlite3')))==1


def test_categories_browse_all_segments_and_keep_unmapped_visible(directory):
    entries=[item(),item('cash',segment='NSE_EQ',instrument='EQUITY',symbol='RELIANCE'),
        item('bse',segment='BSE_EQ',instrument='EQUITY',symbol='RELIANCE',exchange='BSE'),
        item('future',segment='NSE_FNO',instrument='FUTIDX',expiry='2099-01-01'),
        item('option',segment='BSE_FNO',instrument='OPTIDX',expiry='2099-01-01'),
        item('fx',segment='NSE_CURRENCY',instrument='FUTCUR',expiry='2099-01-01'),
        item('mcx',segment='MCX_COMM',instrument='FUTCOM',exchange='MCX',raw_segment='M',expiry='2099-01-01'),
        item('unknown',segment=None,instrument='FUTCOM',exchange='NSE',raw_segment='M',expiry='2099-01-01',reason='Unmapped'),
        item('old',segment='NSE_FNO',instrument='FUTIDX',expiry='2000-01-01')]
    seed(directory.collector,entries,'b'*64)
    counts={'all':8,'indices':1,'cash':2,'futures':4,'options':1,'currency':1,'commodities':2,'unmapped':1}
    for category,count in counts.items():assert directory.search(category=category)['matches']==count
    assert directory.search()['items'][0]['key']=='index'
    assert directory.search(category='futures',active_only=False)['matches']==5
    assert directory.overview()['counts']==counts
    with pytest.raises(HTTPException):directory.search(category="cash' OR 1=1")


def test_browse_prices_use_exact_segment_and_do_not_invent_day_change(directory):
    items=[item('cash',segment='NSE_EQ',instrument='EQUITY',symbol='RELIANCE'),
           item('bse',segment='BSE_EQ',exchange='BSE',instrument='EQUITY',symbol='RELIANCE')]
    seed(directory.collector,items,'b'*64);at=datetime.now(timezone.utc).isoformat()
    directory.collector.store.put_document('system/latest/live',{'rows':{'1:13':{'segment':'NSE_EQ','security_id':'13','ltp':123,'received_at':at,'trade_at':at,'volume':20},
        '4:13':{'segment':'BSE_EQ','security_id':'13','ltp':456,'received_at':at,'trade_at':at}}},0)
    result=directory.search(include_quotes=True)
    quotes={i['key']:i['saved_quote'] for i in result['items']}
    assert quotes['cash']['price']==123 and quotes['bse']['price']==456
    assert 'change' not in quotes['cash'] and 'change_pct' not in quotes['cash']
    assert directory.overview('bse,cash')['watchlist'][0]['saved_quote']['price']==456
    with pytest.raises(HTTPException):directory.overview('../private')
    with pytest.raises(HTTPException):directory.overview(','.join(['cash']*31))


def test_bad_receipts_and_wrong_derivative_expiry_cannot_be_presented_as_prices(directory):
    now=datetime.now(timezone.utc).isoformat()
    items=[item('old',instrument='FUTIDX',segment='NSE_FNO',expiry='2099-01-01'),item('bad',security_id='20')]
    seed(directory.collector,items,'b'*64)
    directory.collector.store.put_document('system/latest/live',{'rows':{
        '2:13':{'segment':'NSE_FNO','security_id':'13','ltp':1,'received_at':now,'expiry':'2098-01-01'},
        '0:20':{'segment':'IDX_I','security_id':'20','ltp':2,'received_at':'2099-01-01T00:00:00+00:00'}}},0)
    assert directory.saved_quotes(items)=={}


def test_price_read_failure_does_not_hide_the_instrument_catalogue(directory,monkeypatch):
    original=directory.collector.store.document
    def document(key):
        if key.startswith('system/latest/'):raise OSError('Store temporarily unavailable')
        return original(key)
    monkeypatch.setattr(directory.collector.store,'document',document)
    assert directory.search(include_quotes=True)['items'][0]['key']=='index'
    result=directory.overview()
    assert result['catalogue_rows']==1 and result['snapshot_read_errors']==4


def test_saved_chart_reads_exact_instrument_and_interval_without_provider(directory):
    from test_collection import candles
    item_=directory.resolve('index',SHA)
    task={'family':'intraday','item':item_,'variant':{'interval':'1'},'from':'2026-10-21','to':'2026-10-23'}
    directory.collector.persist_candles(task,{'raw':candles('2026-10-22')})
    result=directory.saved_history('index',SHA,date(2026,10,21),date(2026,10,23))
    assert result['rows']==1 and result['candles'][0]['close']==11
    assert result['available_days']==['2026-10-22'] and result['absent_partitions']==['2026-10-21']
    assert result['coverage_verified'] is False
    assert directory.saved_history('index',SHA,date(2026,10,21),date(2026,10,23),interval=5)['rows']==0
    assert directory.collector.store.document('control/lease')[0] is None
    with pytest.raises(HTTPException):directory.saved_history('index',SHA,date(2026,1,1),date(2026,10,23))


def test_saved_chart_excludes_corrupt_identity_invalid_rows_and_conflicting_timestamps(directory):
    from test_collection import candles
    task={'family':'intraday','item':directory.resolve('index',SHA),'variant':{'interval':'1'},'from':'2026-10-21','to':'2026-10-23'}
    quality=directory.collector.persist_candles(task,{'raw':candles('2026-10-22')})
    key='candles/'+quality['series']+'/2026-10-22';doc,version=directory.collector.store.document(key)
    row=next(iter(doc['candles'].values()));doc['candles']['conflict']={**row,'close':10.5}
    doc['candles']['invalid']={**row,'timestamp':True}
    directory.collector.store.put_document(key,doc,version)
    result=directory.saved_history('index',SHA,date(2026,10,21),date(2026,10,23))
    assert result['rows']==0 and result['conflicting_timestamps']==1 and result['invalid_excluded']==1
    doc,version=directory.collector.store.document(key);doc['instrument']={**doc['instrument'],'segment':'BSE_EQ'}
    directory.collector.store.put_document(key,doc,version)
    result=directory.saved_history('index',SHA,date(2026,10,21),date(2026,10,23))
    assert result['rows']==0 and result['available_days']==[] and result['invalid_excluded']==1


def test_previous_close_source_is_used_for_change_only_when_available(directory):
    at=datetime.now(timezone.utc).isoformat()
    directory.collector.store.put_document('system/latest/live',{'rows':{'0:13':{'segment':'IDX_I','security_id':'13','ltp':22600,'previous_close':22687.65,'received_at':at,'trade_at':at}}},0)
    quote=directory.overview()['indices'][0]['saved_quote']
    assert round(quote['change'],2)==-87.65 and round(quote['change_pct'],2)==-0.39
    assert quote['previous_close']==22687.65


def test_reused_derivative_security_id_does_not_show_another_expiry_history(directory):
    from test_collection import candles
    previous=item('future',segment='NSE_FNO',instrument='FUTIDX',expiry='2099-01-01')
    seed(directory.collector,[previous],'b'*64)
    task={'family':'intraday','item':previous,'variant':{'interval':'1'},'from':'2026-10-21','to':'2026-10-23'}
    directory.collector.persist_candles(task,{'raw':candles('2026-10-22',{'open_interest':[100]})})
    seed(directory.collector,[{**previous,'expiry':'2100-01-01'}],'c'*64)
    result=directory.saved_history('future','c'*64,date(2026,10,21),date(2026,10,23))
    assert result['rows']==0 and result['invalid_excluded']==1


def test_home_matches_trading_symbols_with_legal_names_and_next50_alias(directory):
    seed(directory.collector,[item(),item('next',symbol='NIFTYNXT50',security_id='38'),
        item('reliance',symbol='RELIANCE INDUSTRIES LTD',underlying='RELIANCE',segment='NSE_EQ',instrument='EQUITY',security_id='2885'),
        item('maruti',symbol='MARUTI SUZUKI INDIA LTD',underlying='MARUTI',segment='NSE_EQ',instrument='EQUITY',security_id='10999')],'c'*64)
    result=directory.overview()
    assert [i['key'] for i in result['watchlist']]==['maruti','reliance']
    assert [i['key'] for i in result['indices']]==['index','next']

def test_home_fallback_prefers_cash_stocks(directory):
    seed(directory.collector,[item(),item('cash',symbol='OTHER COMPANY',segment='NSE_EQ',instrument='EQUITY')],'d'*64)
    assert directory.overview()['watchlist'][0]['key']=='cash'


def test_mapping_review_reconciles_current_expired_and_all_contracts(directory):
    entries=[item('current',segment=None,instrument='OPTFUT',expiry='2099-01-01',reason='NSE_M unverified'),
             item('expired',segment=None,instrument='OPTFUT',expiry='2000-01-01',reason='NSE_M unverified')]
    seed(directory.collector,entries,'e'*64)
    data=directory.search(category='unmapped')
    assert data['matches']==1
    assert data['mapping_review']=={'total':2,'current':1,'expired':1,'state':'API_SEGMENT_UNVERIFIED'}
    assert directory.search(category='unmapped',active_only=False)['matches']==2
    assert directory.overview()['mapping_review']==data['mapping_review']


def test_date_wise_saved_coverage_distinguishes_missing_minutes_and_unknown_calendar(directory):
    from test_collection import candles
    task={'family':'intraday','item':directory.resolve('index',SHA),'variant':{'interval':'1'},'from':'2026-10-19','to':'2026-10-20'}
    directory.collector.persist_candles(task,{'raw':candles('2026-10-19')})
    result=directory.saved_history('index',SHA,date(2026,10,19),date(2026,10,21))
    days={d['date']:d for d in result['session_coverage']}
    assert days['2026-10-19']['received_rows']==1 and days['2026-10-19']['missing_minutes']==374
    assert days['2026-10-19']['session_grid_complete'] is False
    assert days['2026-10-20']['state']=='NO_REGULAR_SESSIONS'
    seed(directory.collector,[item('bse',exchange='BSE',segment='BSE_EQ',raw_segment='E',instrument='EQUITY')],'b'*64)
    result=directory.saved_history('bse','b'*64,date(2026,10,19),date(2026,10,20))
    assert result['session_coverage'][0]['calendar_verified'] is False
    assert result['session_coverage'][0]['missing_minutes'] is None
