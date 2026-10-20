import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,date,timezone
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from store import Store
from research import momentum,workspace,saved_candles,RiskInput
from paper_ledger import PaperOpen,PaperClose,open_trade,close_trade,view
from session_chart import future_vwap
from request_review import diagnose
from provider import ProviderError
import server

def clock():return datetime.fromisoformat('2026-10-12T10:00:00+05:30')
def bars(n=40):
    base=int(clock().replace(hour=9,minute=15).timestamp())
    return [dict(timestamp=base+i*60,open=100+i,high=102+i,low=99+i,close=101+i,volume=100) for i in range(n)]
def raw(rows):return {k:[r[k] for r in rows] for k in ('timestamp','open','high','low','close','volume')}
def risk():
    return RiskInput(entry=100,stop=90,target=120,risk_rupees=2000,capital=100000,daily_loss_cap=5000,realised_loss=0,open_risk=0,
        lot_size=75,lot_source='exchange master',lot_verified_on=date(2026,10,12),brokerage_roundtrip=40,
        exchange_roundtrip_per_lot=5,stt_exit_rate=.0015,sebi_turnover_rate=.000001,gst_rate=.18,stamp_entry_rate=.00003,
        slippage_per_unit=1,spread_per_unit=.5,costs_effective_on=date(2026,10,1),costs_source='explicit test assumptions',expiry=date(2026,10,15))
def master():return {'123':{'security_id':'123','underlying':'nifty','expiry':'2026-10-15','strike':100,'side':'CE','lot_size':75}}
def opening(tid='a'*32):return PaperOpen(id=tid,symbol='nifty',security_id='123',setup='opening_range',reason='Manual hypothesis',deviations='Data gates unavailable; recorded for review only',risk=risk())

def test_current_provider_document_replaces_old_today_rows_and_setup_uses_it(tmp_path):
    s=Store(tmp_path/'s.db');old=bars(40);old[0]['close']=100;old[0]['high']=102
    s.add('dhan_history',{'metadata':{'kind':'index','index':'nifty','fetched_at':clock().isoformat()},'raw':raw(old)})
    current=bars(45)
    record={'symbol':'nifty','received_at':clock().isoformat(),'request':{'fromDate':'2026-10-12 09:15:00','toDate':'2026-10-12 10:00:00'},'raw':raw(current)}
    s.put_document('research/session/nifty',record,0)
    w=workspace(s,'nifty',clock())
    assert len(w['history']['candles'])==45 and w['history']['candles'][0]['close']==101
    assert w['candle_freshness']['state']=='FRESH'
    assert 'OPENING_RANGE_INCOMPLETE' not in w['setups'][0]['missing']
    # A replay before receipt must not see this later-fetched source document.
    assert not any(str(x).startswith('research/session') for x in saved_candles(s,'nifty',clock()-timedelta(minutes=1))['source_record_ids'])

def test_momentum_flat_and_monotone_edge_cases():
    assert momentum(bars(14))['rsi14'] is None
    rising=momentum(bars(40));assert rising['rsi14']==100 and rising['adx14']==100
    falling=deepcopy(bars(40))
    for i,r in enumerate(falling):r.update(open=200-i,high=202-i,low=199-i,close=201-i)
    assert momentum(falling)['rsi14']==0 and momentum(falling)['adx14']==100
    flat=[{**r,'open':100,'high':100,'low':100,'close':100} for r in bars(40)]
    assert momentum(flat)['rsi14']==50 and momentum(flat)['adx14']==0

def test_futures_candle_vwap_requires_complete_volume_and_current_data():
    c=clock();rs=bars(45);record={'symbol':'nifty','security_id':'321','expiry':'2026-10-29','received_at':c.isoformat(),'raw':raw(rs)}
    report=future_vwap(record,c)
    expected=sum((r['high']+r['low']+r['close'])/3*r['volume'] for r in rs)/sum(r['volume'] for r in rs)
    assert report['value']==pytest.approx(expected)
    record['raw']=raw(rs[1:]);assert future_vwap(record,c)['value'] is None
    record['raw']=raw(rs);assert future_vwap(record,c+timedelta(minutes=3))['value'] is None

def test_paper_entry_exit_are_idempotent_costed_and_do_not_become_validated_results(tmp_path):
    s=Store(tmp_path/'s.db');p=opening()
    a=open_trade(s,p,master(),clock());assert a['quantity']==150 and a['state']=='OPEN'
    assert open_trade(s,p,{},clock())==a # Retry remains valid after master expiry/rotation.
    exit=PaperClose(exit_premium=110,reason='Target assumption',deviations='')
    r=close_trade(s,p.id,exit,clock()+timedelta(minutes=5))
    assert r['gross_hypothetical_pnl']==1500 and 0<r['hypothetical_net_pnl']<1500
    assert close_trade(s,p.id,exit,clock()+timedelta(minutes=6))==r
    assert view(s)['recorded_closed_trades']==1 and view(s)['validated_trades']==0
    with pytest.raises(HTTPException):close_trade(s,p.id,PaperClose(exit_premium=120,reason='Changed assumption'),clock())

def test_zero_premium_exit_and_exact_contract_identity(tmp_path):
    s=Store(tmp_path/'s.db');p=opening()
    wrong=master();wrong['123']['underlying']='banknifty'
    with pytest.raises(HTTPException):open_trade(s,p,wrong,clock())
    p.deviations=''
    with pytest.raises(HTTPException):open_trade(s,p,master(),clock())
    p.deviations='Explicitly record unconfirmed hypothesis';a=open_trade(s,p,master(),clock())
    r=close_trade(s,p.id,PaperClose(exit_premium=0,reason='Worthless hypothetical expiry'),clock())
    assert r['hypothetical_net_pnl']<-a['quantity']*100

def test_authoritative_open_reserve_prevents_concurrent_daily_cap_bypass(tmp_path):
    s=Store(tmp_path/'s.db')
    def attempt(tid):
        p=opening(tid);p.risk.daily_loss_cap=1000;p.risk.risk_rupees=1000
        try:return open_trade(s,p,master(),clock())
        except HTTPException:return None
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(attempt,['a'*32,'b'*32]))
    assert sum(r is not None for r in result)==1

def test_diagnosis_preserves_checkpoint_and_reports_ambiguous_907(tmp_path):
    s=Store(tmp_path/'s.db');item={'security_id':'13','segment':'IDX_I','instrument':'INDEX','symbol':'NIFTY'}
    job={'state':'request_review','current':{'item':item,'family':'daily','from':'2021-10-19','to':'2022-10-19'},'request_error':{'code':'DH-907'}}
    s.put_document('jobs/'+'a'*32,job,0);s.put_document('page',{'items':[item]},0)
    class Provider:
        async def post(self,*args):raise ProviderError('No data or wrong parameters',code='DH-907',status=400)
    collector=SimpleNamespace(store=s,provider=Provider(),get_job=lambda _:deepcopy(job),catalogue=lambda:{'pages':[{'group':'IDX_I-INDEX','key':'page'}]})
    result=asyncio.run(diagnose(collector,'a'*32))
    assert len(result['probes'])==2 and result['conclusion']=='SOURCE_REQUEST_REMAINS_UNRESOLVED'
    assert s.document('jobs/'+'a'*32)[0]==job and result['checkpoint_changed'] is False

def test_paper_read_and_writes_require_existing_admin(tmp_path,monkeypatch):
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for key in ('K_SERVICE','THETA_GCS_BUCKET','THETA_TOKEN_ADMIN_HASH'):monkeypatch.delenv(key,raising=False)
    c=TestClient(server.create_app(tmp_path/'a.db'))
    assert c.post('/api/research/paper/list',json={}).status_code==503
    assert c.post('/api/research/paper',json=opening().model_dump(mode='json')).status_code==503


def test_new_paper_entry_requires_target_and_retains_fixed_stop_target(tmp_path):
    s=Store(tmp_path/'target.db');p=opening();p.risk.target=None
    with pytest.raises(HTTPException,match='explicit premium target'):open_trade(s,p,master(),clock())
    p.risk.target=120;t=open_trade(s,p,master(),clock())
    assert (t['stop_premium'],t['target_premium'])==(90,120)
    result=close_trade(s,p.id,PaperClose(exit_premium=120,reason='Explicit target scenario'),clock()+timedelta(minutes=1))
    report=view(s)
    assert result['hypothetical_net_pnl']==2688.51
    assert result['realised_r']==pytest.approx(result['hypothetical_net_pnl']/t['risk_plan']['planned_stop_loss'],abs=.0001)
    assert report['metrics']['expectancy_rupees']==2688.51
    assert report['metrics']['closed_trades']==1 and not report['metrics']['validated']
    assert report['validated_trades']==0


def test_hypothetical_drawdown_uses_exit_order_and_costs_are_not_subtracted_twice():
    from paper_ledger import hypothetical_metrics
    def trade(tid,minute,net,reserve):return dict(id=tid,closed_at=f'2026-10-12T10:{minute}:00+05:30',hypothetical_net_pnl=net,risk_plan={'planned_stop_loss':reserve})
    m=hypothetical_metrics([trade('a','10',100,50),trade('b','05',-40,40),trade('c','15',-80,80)])
    assert m['max_drawdown_rupees']==80 and m['expectancy_rupees']==-6.67
    assert m['average_r']==0 and m['profit_factor']==.8333 and m['win_rate']==.3333
    assert hypothetical_metrics([])['expectancy_rupees'] is None


def test_full_api_risk_to_exact_contract_paper_close_and_evaluation(tmp_path,monkeypatch):
    from runtime import IST
    today=datetime.now(IST).date();p=opening();p.risk.lot_verified_on=today;p.risk.expiry=today+timedelta(days=7)
    contracts=master();contracts['123']['expiry']=p.risk.expiry.isoformat()
    async def catalog():pass
    monkeypatch.setenv('THETA_AUTH_MODE','public')
    for key in ('K_SERVICE','THETA_GCS_BUCKET'):monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr(server,'require_admin',lambda request:'test-authorized')
    monkeypatch.setattr(server,'MarketBoard',lambda *args:SimpleNamespace(catalog=catalog,options=contracts))
    client=TestClient(server.create_app(tmp_path/'flow.db'))
    plan=client.post('/api/research/risk',json=p.risk.model_dump(mode='json'))
    assert plan.status_code==200 and plan.json()['target_premium']==120
    opened=client.post('/api/research/paper',json=p.model_dump(mode='json'))
    assert opened.status_code==200 and opened.json()['contract']['security_id']=='123'
    closed=client.post('/api/research/paper/'+p.id+'/close',json={'exit_premium':120,'reason':'Manual target scenario','deviations':''})
    assert closed.status_code==200
    ledger=client.post('/api/research/paper/list',json={}).json()
    assert ledger['metrics']['expectancy_rupees']==closed.json()['hypothetical_net_pnl']
    assert ledger['trades'][0]['target_premium']==120 and ledger['validated_trades']==0
