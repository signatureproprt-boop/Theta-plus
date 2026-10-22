from copy import deepcopy
from datetime import datetime, timedelta
import pytest
from indicator_engine import build, VERSION
from research import workspace, indicators, setup_rules
from store import Store

START=datetime.fromisoformat('2026-10-22T09:15:00+05:30')
def candles(n,prices=None):
    return [dict(timestamp=int((START+timedelta(minutes=i)).timestamp()),open=p,high=p+1,low=p-1,close=p,volume=0,
                 closed_at=(START+timedelta(minutes=i+1)).isoformat(),received_at=(START+timedelta(minutes=i+1)).isoformat())
            for i,p in enumerate(prices if prices is not None else [100+i for i in range(n)])]
def calc(rows,minutes=1,at=None):
    return build(rows,at or START+timedelta(minutes=len(rows)))['frames'][str(minutes)]

def test_wilder_reference_rsi_and_sma_seed_ema():
    prices=[44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28,46.00,46.03,46.41,46.22,45.64,46.21]
    r=calc(candles(len(prices),prices))
    assert 'value' not in r['rsi14'][13]
    assert r['rsi14'][14]['value']==pytest.approx(70.4641350211)
    assert r['rsi14'][15]['value']==pytest.approx(66.2496185536)
    assert r['ema20'][19]['value']==pytest.approx(sum(prices[:20])/20)
    assert r['ema20'][20]['value']==pytest.approx(sum(prices[:20])/20+(prices[20]-sum(prices[:20])/20)*2/21)

def test_adx_seed_and_flat_market_denominators():
    rising=calc(candles(55))
    assert 'value' not in rising['adx14'][26]
    assert rising['adx14'][27]['value']==100
    assert rising['snapshot']['rsi14']==100 and rising['snapshot']['bias']=='BULLISH'
    flat=calc(candles(55,[100]*55))
    assert flat['snapshot']['rsi14']==50 and flat['snapshot']['adx14']==0
    assert flat['snapshot']['bias']=='SIDEWAYS'

def test_gap_resets_every_indicator_and_cannot_reuse_old_latest_value():
    rows=candles(100);rows.pop(70)
    r=calc(rows,at=START+timedelta(minutes=100))
    assert r['bars'][70]=={'time':int(START.timestamp())+70*60}
    assert 'value' not in r['ema20'][89] and r['ema20'][90]['value']>0
    assert 'value' not in r['rsi14'][84] and r['rsi14'][85]['value']==100
    assert 'value' not in r['adx14'][97] and r['adx14'][98]['value']==100
    assert r['snapshot']['ema50'] is None
    trailing=calc(rows,at=START+timedelta(minutes=101))
    assert trailing['snapshot']['state']=='GAP' and trailing['snapshot']['rsi14'] is None

def test_forming_future_offgrid_conflicts_and_input_are_not_changed():
    rows=candles(61);before=deepcopy(rows);at=START+timedelta(minutes=60)
    a=build(rows,at);rows[-1].update(close=9999,high=9999)
    assert build(rows,at)==a
    assert a['frames']['60']['snapshot']['bars']==1
    assert before[0]==rows[0]
    bad=deepcopy(before[:60]);bad.append({**bad[0],'close':100.5});bad.append({**bad[1],'timestamp':bad[1]['timestamp']+1})
    r=build(bad,at);assert r['conflicts']==1 and r['excluded']==1
    assert r['orb']['state']=='INCOMPLETE'

def test_session_anchor_tail_orb_and_snapshot_use_identical_series():
    rows=candles(375);at=START+timedelta(minutes=375)
    pack=build(rows,at)
    for key,count in [('1',375),('5',75),('15',25),('60',6)]:
        frame=pack['frames'][key];assert len(frame['bars'])==count
        for name in ('ema20','ema50','rsi14','adx14'):
            assert frame['snapshot'][name]==frame[name][-1].get('value')
    assert pack['orb']['high']==115 and pack['orb']['low']==99
    assert pack['levels'][2]['price']==pack['orb']['high']
    assert pack['frames']['60']['bars'][-1]['time']==int(START.timestamp())+300*60
    assert indicators(rows,at)=={k:v['snapshot'] for k,v in pack['frames'].items()}

def test_workspace_and_setup_share_engine_values_without_provider_requests(tmp_path):
    store=Store(tmp_path/'db');rows=candles(100);at=START+timedelta(minutes=100)
    store.add('dhan_history',{'metadata':{'kind':'index','index':'nifty','fetched_at':at.isoformat()},'raw':{k:[r[k] for r in rows] for k in ('timestamp','open','high','low','close','volume')}})
    data=workspace(store,'nifty',at)
    assert data['indicator_chart']['version']==VERSION
    assert all(s['indicators']==data['indicators'] for s in data['setups'])
    assert data['futures_context']['value'] is None
    assert all(s['state']=='DATA_UNAVAILABLE' for s in data['setups'])
    rows.pop(50);assert 'CANDLE_GAPS' in setup_rules(rows,at,'opening_range')['missing']

def test_overnight_warmup_carries_only_across_complete_session_boundaries():
    rows=candles(375);next_start=START+timedelta(days=1)
    tomorrow=[{**r,'timestamp':r['timestamp']+86400} for r in candles(20)]
    r=calc(rows+tomorrow,at=next_start+timedelta(minutes=20))
    assert r['snapshot']['consecutive_bars']==395
    broken=calc(rows[:-1]+tomorrow,at=next_start+timedelta(minutes=20))
    assert broken['snapshot']['consecutive_bars']==20 and broken['snapshot']['ema50'] is None


def test_futures_vwap_keeps_exact_identity_and_hides_stale_or_missing_minutes():
    from session_chart import future_vwap
    rows=candles(3,[100,110,120]);volumes=[1,2,3]
    at=START+timedelta(minutes=3)
    record={'symbol':'nifty','security_id':'exact-future-1','expiry':'2026-10-27','received_at':at.isoformat(),
            'raw':{k:[r[k] for r in rows] for k in ('timestamp','open','high','low','close')}}
    record['raw']['volume']=volumes
    result=future_vwap(record,at)
    assert result['value']==pytest.approx((100+220+360)/6)
    assert result['security_id']=='exact-future-1' and result['expiry']=='2026-10-27'
    assert future_vwap(record,at+timedelta(minutes=2))['value'] is None
    record['raw']={k:v[:1]+v[2:] for k,v in record['raw'].items()}
    assert future_vwap(record,at)['value'] is None


def test_missing_entire_regular_session_resets_but_documented_weekend_does_not():
    rows=candles(375)
    monday=[{**r,'timestamp':r['timestamp']+4*86400} for r in candles(20)]
    # Thursday -> Monday omits Friday, so no overnight EMA may bridge it.
    missing=calc(rows+monday,at=START+timedelta(days=4,minutes=20))
    assert missing['snapshot']['consecutive_bars']==20 and missing['snapshot']['ema50'] is None
    friday=[{**r,'timestamp':r['timestamp']+86400} for r in candles(375)]
    weekend=calc(friday+monday,at=START+timedelta(days=4,minutes=20))
    assert weekend['snapshot']['consecutive_bars']==395


def test_documented_holiday_minutes_cannot_enter_indicators():
    holiday=[{**r,'timestamp':r['timestamp']-2*86400} for r in candles(60)]
    pack=build(holiday,START-timedelta(days=2)+timedelta(minutes=60))
    assert pack['excluded']==60 and pack['frames']['1']['snapshot']['bars']==0
