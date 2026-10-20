from copy import deepcopy
from datetime import datetime,date,timedelta,timezone
import hashlib
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
import research as r
from runtime import IST
from store import Store
import server

def clock(value='2026-10-12T10:00:00+05:30'):
    return datetime.fromisoformat(value).astimezone(timezone.utc)

def bars(n=60,start=None):
    start=start or clock('2026-10-12T09:15:00+05:30')
    return [dict(timestamp=int((start+timedelta(minutes=i)).timestamp()),open=100+i,high=102+i,low=99+i,close=101+i,volume=100,
                 closed_at=(start+timedelta(minutes=i+1)).isoformat(),received_at=(start+timedelta(minutes=i+1)).isoformat()) for i in range(n)]

def save(store,rows):
    return store.add('dhan_history',{'metadata':{'kind':'index','index':'nifty','fetched_at':clock().isoformat()},
                  'raw':{k:[x[k] for x in rows] for k in ('timestamp','open','high','low','close','volume')}})

def chain(exchange=False):
    at=clock();m={'index':'nifty','expiry':'2026-10-15','fetched_at':at.isoformat()}
    if exchange:m['exchange_at']=(at-timedelta(seconds=2)).isoformat()
    return {'metadata':m,'raw':{'data':{'last_price':100,'oc':{'100':{
        'ce':dict(security_id=1,last_price=10,top_bid_price=9.95,top_ask_price=10.05,oi=200,previous_oi=190,volume=100,implied_volatility=15,greeks={'delta':.5}),
        'pe':dict(security_id=2,last_price=10,top_bid_price=10.1,top_ask_price=10,oi=400,previous_oi=420,volume=300)}}}}}

@pytest.mark.parametrize('at,received,expected',[(None,None,'TIME_UNVERIFIED'),('2026-10-12T09:59:58+05:30',None,'INGEST_TIME_MISSING'),
    ('2026-10-12T09:59:58+05:30','2026-10-12T10:00:00+05:30','FRESH'),
    ('2026-10-12T09:59:40+05:30','2026-10-12T10:00:00+05:30','STALE'),
    ('2026-10-12T10:00:01+05:30','2026-10-12T10:00:02+05:30','FUTURE_TIMESTAMP')])
def test_freshness_fails_closed(at,received,expected):
    assert r.freshness(at,received,clock(),10)['state']==expected

def test_naive_timestamp_does_not_establish_freshness():
    assert r.stamp('2026-10-12T10:00:00') is None

def test_complete_session_anchored_htf_and_no_partial_bars():
    data=bars(61)
    assert len(r.aggregate(data,60,clock('2026-10-12T10:15:00+05:30')))==1
    assert not r.aggregate(data,60,clock('2026-10-12T10:14:59+05:30'))
    assert not r.aggregate(data[:20]+data[21:],60,clock('2026-10-12T10:30:00+05:30'))
    assert r.aggregate(data,5,clock())[0]['timestamp']==data[0]['timestamp']

def test_saved_history_excludes_conflicts_and_open_candles(tmp_path):
    s=Store(tmp_path/'s.db');rows=bars(3);save(s,rows)
    other=deepcopy(rows);other[0]['close']=100;save(s,other)
    loaded=r.saved_candles(s,'nifty',clock('2026-10-12T09:17:00+05:30'))
    assert loaded['conflicting_minutes']==1
    assert [x['timestamp'] for x in loaded['candles']]==[rows[1]['timestamp']]

def test_receipt_is_not_quote_time_and_pcr_never_forecasts():
    result=r.chain_analysis(chain(),clock())
    assert result['status']=='TIME_UNVERIFIED'
    assert result['pcr_oi']==2 and result['pcr_volume']==3
    assert all(c['greeks'] is None and c['iv'] is None and not c['identity_verified'] for c in result['contracts'])
    assert result['pcr_history'] is None and result['iv_percentile'] is None

def test_spread_crossed_book_and_identity_verification():
    result=r.chain_analysis(chain(True),clock(),{'1':dict(expiry='2026-10-15',strike=100,side='CE',lot_size=75)})
    ce,pe=result['contracts']
    assert ce['spread_pct']==pytest.approx(1) and ce['identity_verified'] and ce['lot_size']==75
    assert pe['spread_pct'] is None and pe['liquidity']=='INVALID_QUOTE'
    wrong=r.chain_analysis(chain(True),clock(),{'1':dict(expiry='2026-10-22',strike=100,side='CE',lot_size=75)})
    assert not wrong['contracts'][0]['identity_verified']

def test_missing_chain_side_or_zero_denominator_hides_pcr():
    raw=chain();del raw['raw']['data']['oc']['100']['pe']
    assert r.chain_analysis(raw,clock())['pcr_oi'] is None
    raw=chain();raw['raw']['data']['oc']['100']['ce']['oi']=0
    assert r.chain_analysis(raw,clock())['pcr_oi'] is None

def test_stale_greeks_hidden_even_when_master_verified():
    raw=chain(True);raw['metadata']['exchange_at']=(clock()-timedelta(seconds=11)).isoformat()
    assert r.chain_analysis(raw,clock())['contracts'][0]['greeks'] is None

def test_setup_cannot_confirm_without_data_context_and_quote():
    out=r.setup_rules(bars(),clock(),'opening_range')
    assert out['state']=='DATA_UNAVAILABLE'
    assert {'BREADTH_UNAVAILABLE','EVENT_CONTEXT_UNAVAILABLE','OPTION_QUOTE_UNAVAILABLE'}.issubset(out['missing'])

def test_future_candle_mutation_does_not_change_decision():
    data=bars();before=r.setup_rules(data,clock(),'opening_range')
    for row in data[46:]:row.update(close=999999,high=999999)
    assert r.setup_rules(data,clock(),'opening_range')==before

def test_spread_side_expiry_and_liquidity_reason_codes():
    contract=dict(identity_verified=True,lot_size=75,freshness='FRESH',side='PE',spread_pct=5,oi=1,volume=1,expiry='2026-10-12')
    out=r.setup_rules(bars(),clock(),'trend_pullback',contract=contract,breadth=.8,context_fresh=True,room_r=2)
    assert {'SPREAD_TOO_WIDE','LIQUIDITY_TOO_LOW','EXPIRY_DAY_BLOCKED'}.issubset(out['failed'])

def test_opening_break_retest_trigger_can_confirm_only_with_all_gates(monkeypatch):
    data=bars(30)
    for i,row in enumerate(data):
        row.update(open=100,high=100.2,low=99.8,close=100)
        if 15<=i<20:row.update(open=100.2,high=101,low=100.2,close=100.8)
        if 20<=i<25:row.update(open=100.3,high=100.5,low=100.2,close=100.3)
        if i>=25:row.update(open=100.4,high=101.2,low=100.3,close=101)
    # Isolate price-event logic; warm-up/alignment has separate complete-bucket tests.
    monkeypatch.setattr(r,'indicators',lambda *a:{'60':{'bias':'BULLISH'},'15':{'bias':'BULLISH'}})
    contract=dict(identity_verified=True,lot_size=75,freshness='FRESH',side='CE',spread_pct=.5,volume=1000,oi=1000,expiry='2026-10-15')
    out=r.setup_rules(data,clock('2026-10-12T09:45:00+05:30'),'opening_range',contract=contract,breadth=.8,context_fresh=True,room_r=2)
    assert out['state']=='CONFIRM' and 'RETEST_HOLD_OK' in out['passed']
    contract['spread_pct']=2
    assert r.setup_rules(data,clock('2026-10-12T09:45:00+05:30'),'opening_range',contract=contract,breadth=.8,context_fresh=True,room_r=2)['state']=='NO_TRADE'

def risk(**changes):
    return r.RiskInput(entry=100,stop=90,risk_rupees=2000,capital=100000,daily_loss_cap=5000,realised_loss=0,open_risk=0,
        lot_size=75,lot_source='exchange master',lot_verified_on=date(2026,10,12),brokerage_roundtrip=40,
        exchange_roundtrip_per_lot=5,stt_exit_rate=.0015,sebi_turnover_rate=.000001,gst_rate=.18,stamp_entry_rate=.00003,
        slippage_per_unit=1,spread_per_unit=.5,costs_effective_on=date(2026,10,1),costs_source='explicit test assumptions',expiry=date(2026,10,15),**changes)

def test_quantity_is_lots_times_lot_size_and_includes_costs():
    p=risk();out=r.risk_plan(p,clock())
    assert out['lots']==2 and out['quantity']==150
    assert 1500<out['planned_stop_loss']<=p.risk_rupees
    assert out['estimated_costs']>0 and out['premium_paid']==15000

def test_daily_cap_and_open_risk_reduce_quantity():
    p=risk();p.realised_loss=4000;p.open_risk=900
    out=r.risk_plan(p,clock());assert out['quantity']==0 and out['state']=='NO_TRADE'

def test_costs_cannot_increase_affordable_quantity():
    p=risk();base=r.risk_plan(p,clock());p.slippage_per_unit=10
    assert r.risk_plan(p,clock())['lots']<base['lots']

def test_stale_lot_expired_contract_and_future_tariff_block_sizing():
    for field,value,reason in [('lot_verified_on',date(2026,10,9),'LOT_METADATA_NOT_CURRENT'),('expiry',date(2026,10,9),'CONTRACT_EXPIRED'),('costs_effective_on',date(2026,11,1),'COSTS_NOT_EFFECTIVE')]:
        p=risk();setattr(p,field,value);out=r.risk_plan(p,clock());assert out['quantity']==0 and reason in out['reason_codes']

def test_nan_and_invalid_stop_rejected():
    p=risk().model_dump();p['entry']=float('nan')
    with pytest.raises(ValidationError):r.RiskInput(**p)
    p=risk().model_dump();p['stop']=101
    with pytest.raises(ValidationError):r.RiskInput(**p)

def test_replay_event_clock_blocks_future_and_option_pnl(tmp_path):
    s=Store(tmp_path/'s.db');rows=bars();save(s,rows)
    p=r.ReplayInput(symbol='nifty',start=date(2026,10,12),end=date(2026,10,13),clock=clock())
    before=r.replay_report(s,p)
    assert len(before['events'])==9 and before['option_pnl'] is None and before['metrics'] is None
    assert all(r.stamp(e['clock'])<=p.clock for e in before['events'])
    future=deepcopy(rows[46:]);future[0]['close']=999;future[0]['high']=999;save(s,future)
    after=r.replay_report(s,p)
    assert before['events']==after['events'] and before['latest']==after['latest']

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for name in ('K_SERVICE','THETA_GCS_BUCKET','THETA_DHAN_CREDENTIALS_SECRET','THETA_DHAN_TOKEN_SECRET'):
        monkeypatch.delenv(name,raising=False)
    salt='ab'*16;digest=hashlib.pbkdf2_hmac('sha256',b'fixture',bytes.fromhex(salt),600000).hex()
    monkeypatch.setenv('THETA_TOKEN_ADMIN_HASH',f'pbkdf2_sha256$600000${salt}${digest}')
    return TestClient(server.create_app(tmp_path/'app.db'))

def test_workspace_is_read_only_and_collect_jobs_unchanged(client):
    s=client.app.state.store;s.put_document('jobs/'+'a'*32,{'id':'a'*32,'state':'running','pages':[]},0)
    original=s.document('jobs/'+'a'*32)
    out=client.get('/api/research/workspace?symbol=nifty');assert out.status_code==200
    assert all(x['state']=='DATA_UNAVAILABLE' for x in out.json()['setups'])
    assert s.document('jobs/'+'a'*32)==original
    assert client.get('/api/research/workspace?symbol=secret').status_code==422

def test_journal_auth_idempotency_xss_and_no_fabricated_trade(client):
    p={'id':'b'*32,'symbol':'nifty','decision':'WAIT','note':'<script>untrusted</script>','deviations':'rule skipped','setup':'opening_range'}
    assert client.post('/api/research/journal',json=p).status_code==403
    h={'X-Theta-Admin-Password':'fixture'}
    a=client.post('/api/research/journal',json=p,headers=h);b=client.post('/api/research/journal',json=p,headers=h)
    assert a.status_code==200 and a.json()==b.json()
    rows=client.get('/api/research/journal').json()['entries'];assert len(rows)==1 and rows[0]['kind']=='OBSERVATION_NOT_TRADE'
    p['note']='different';assert client.post('/api/research/journal',json=p,headers=h).status_code==409
    assert client.get('/api/research/journal?after=../credentials').status_code==422

def test_risk_and_replay_bad_inputs_are_bounded(client):
    assert client.post('/api/research/risk',json={}).status_code==422
    assert client.post('/api/research/replay',json={'symbol':'nifty','start':'2026-01-01','end':'2026-10-01','clock':'2026-10-12T10:00:00+05:30'}).status_code==422

def test_research_navigation_and_assets(client):
    page=client.get('/').text
    for x in ('research','options','quality','risk','replay','paper'):assert 'page-'+x in page
    assert client.get('/static/research.js').status_code==200
    assert client.get('/static/research.css').status_code==200
    assert client.get('/api/overview').status_code==410


def test_observed_room_uses_only_nearest_known_obstacle_and_requires_valid_risk():
    at=clock('2026-10-12T10:00:00+05:30')
    start=int(clock('2026-10-12T09:15:00+05:30').timestamp())
    rows=[{'timestamp':start,'open':100,'high':110,'low':90,'close':100,'volume':1},
          {'timestamp':start+60,'open':100,'high':105,'low':99,'close':104,'volume':1}]
    result=r.price_room(rows,at,'CE',100)
    assert result['price']==110 and result['room_r']==1.5
    assert r.price_room(rows,at,'CE',105) is None
    assert r.price_room(rows,at,'CE',None) is None
    # If price is already beyond all observed highs, do not invent unlimited room.
    rows[-1]['close']=120;assert r.price_room(rows,at,'CE',100) is None


def test_breadth_flats_do_not_count_as_put_participation():
    out=r.setup_rules(bars(),clock(),'trend_pullback',breadth={'fraction_up':.1,'fraction_down':.1},context_fresh=True,room_r=2,
                      indicator_view={'60':{'bias':'BEARISH'},'15':{'bias':'BEARISH'}})
    assert 'PARTICIPATION_WEAK' in out['reason_codes']


def test_future_obstacle_cannot_change_price_room():
    at=clock('2026-10-12T10:00:00+05:30');start=int(clock('2026-10-12T09:15:00+05:30').timestamp())
    rows=[{'timestamp':start,'high':110,'low':90,'close':100}, {'timestamp':start+60,'high':105,'low':99,'close':104}]
    baseline=r.price_room(rows,at,'CE',100)
    assert r.price_room(rows+[{'timestamp':int(at.timestamp())+60,'high':99999,'low':1,'close':1}],at,'CE',100)==baseline


def test_price_trigger_is_visible_without_bypassing_missing_option_or_event_gates(monkeypatch):
    data=bars(30)
    for i,row in enumerate(data):
        row.update(open=100,high=100.2,low=99.8,close=100)
        if 15<=i<20:row.update(open=100.2,high=101,low=100.2,close=100.8)
        if 20<=i<25:row.update(open=100.3,high=100.5,low=100.2,close=100.3)
        if i>=25:row.update(open=100.4,high=101.2,low=100.3,close=101)
    monkeypatch.setattr(r,'indicators',lambda *args:{'60':{'bias':'BULLISH'},'15':{'bias':'BULLISH'}})
    at=clock('2026-10-12T09:45:00+05:30')
    result=r.setup_rules(data,at,'opening_range')
    assert result['price_pattern']['state']=='TRIGGER_OBSERVED'
    assert result['price_pattern']['trigger_price']==100.5
    assert result['price_pattern']['underlying_invalidation']==100.2
    assert result['state']=='DATA_UNAVAILABLE'
    assert {'EVENT_CONTEXT_UNAVAILABLE','OPTION_QUOTE_UNAVAILABLE'}.issubset(result['missing'])
    assert r.setup_rules(data,at+timedelta(minutes=3),'opening_range')['price_pattern']['state']=='DATA_UNAVAILABLE'
    for row in data:row['received_at']=(at+timedelta(days=1)).isoformat()
    replay=r.setup_rules(data,at,'opening_range',pattern_asof_close=True)
    assert replay['price_pattern']['state']=='TRIGGER_OBSERVED'
    assert replay['price_pattern']['clock_basis']=='BAR_CLOSE_REPLAY_ASSUMPTION'
    assert replay['state']=='DATA_UNAVAILABLE' and 'CANDLE_FUTURE_TIMESTAMP' in replay['missing']


def test_target_reward_is_net_of_exit_specific_costs_not_double_deducted():
    p=risk(target=120);result=r.risk_plan(p,clock())
    assert result['quantity']==150 and result['planned_net_reward']==2688.51
    assert result['net_reward_risk_ratio']==pytest.approx(round(2688.51106/result['planned_stop_loss'],4),abs=.0001)
    for target in (90,100,float('nan'),float('inf')):
        with pytest.raises(ValidationError):risk(target=target)
