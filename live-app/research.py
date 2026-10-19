"""Versioned decision support. Never places orders or modifies collection jobs.

Saved observations are evidence, not proof of live freshness. All strategy
calculations operate on closed candles available at an explicit event clock.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from math import floor, isfinite
from typing import Literal
from pydantic import Field, FiniteFloat, field_validator, model_validator
from models import Model, aware
from runtime import IST
from dhan_data import candle_count
from provider import ProviderError

RULE_VERSION='research-1.0'
POLICY={
    'candle':{'max_age_seconds':90,'timestamp':'exchange candle close','required':['exchange_at','received_at','open','high','low','close','volume']},
    'chain':{'max_age_seconds':10,'timestamp':'exchange quote, not server receipt','required':['expiry','strike','side','security_id','bid','ask','volume','oi']},
    'greeks':{'max_age_seconds':10,'stale_action':'hide','required':['source','exchange_at','delta','gamma','theta','vega']},
    'futures_vwap':{'max_age_seconds':90,'required':['symbol','expiry','volume','exchange_at','received_at']},
    'context':{'max_age_seconds':60,'stale_action':'DATA_UNAVAILABLE'},
}

def number(v):
    if isinstance(v,bool):return None
    try:
        x=float(v)
        return x if isfinite(x) else None
    except (ValueError,TypeError):return None

def stamp(v):
    try:
        if isinstance(v,(int,float)) and not isinstance(v,bool):return datetime.fromtimestamp(v,timezone.utc)
        return aware(datetime.fromisoformat(v))
    except (ValueError,TypeError,OverflowError,OSError):return None

def freshness(exchange_at,received_at,clock,max_age):
    exchange,received=stamp(exchange_at),stamp(received_at)
    if exchange is None:return {'state':'TIME_UNVERIFIED','age_seconds':None}
    if received is None:return {'state':'INGEST_TIME_MISSING','age_seconds':None}
    if exchange>received or received>clock or exchange>clock:return {'state':'FUTURE_TIMESTAMP','age_seconds':None}
    age=(clock-exchange).total_seconds()
    return {'state':'FRESH' if age<=max_age else 'STALE','age_seconds':round(age,2)}

def saved_candles(store,symbol,clock=None,record_limit=100):
    """Bounded legacy raw-history view; full-universe job coverage is separate."""
    rows={};conflicts=set();sources=[];invalid=[]
    for saved in store.rows('dhan_history',record_limit):
        data=saved['data'];m=data.get('metadata',{})
        if m.get('kind')!='index' or m.get('index')!=symbol:continue
        raw=data.get('raw',{})
        try:candle_count(raw)
        except (ProviderError,TypeError,KeyError):invalid.append(saved['id']);continue
        sources.append(saved['id'])
        for i,t in enumerate(raw['timestamp']):
            at=stamp(t)
            if at is None or at> (clock or datetime.now(timezone.utc)) or (clock and at+timedelta(minutes=1)>clock):continue
            local=at.astimezone(IST)
            if local.weekday()>4 or not '09:15'<=local.strftime('%H:%M')<'15:30':continue
            row={k:raw[k][i] for k in ('timestamp','open','high','low','close','volume')}
            row.update(exchange_at=at.isoformat(),closed_at=(at+timedelta(minutes=1)).isoformat(),received_at=m.get('fetched_at') or saved['created_at'])
            if t in rows and any(rows[t][k]!=row[k] for k in ('open','high','low','close','volume')):conflicts.add(t)
            else:rows[t]=row
    return {'candles':[v for k,v in sorted(rows.items()) if k not in conflicts],
            'conflicting_minutes':len(conflicts),'invalid_record_ids':invalid,'source_record_ids':sources,
            'read_scope':f'Latest {record_limit} retained dhan_history records; collection job archives are separate',
            'calendar':'Weekday regular-session filter only; holidays/special sessions not certified'}

def aggregate(candles,minutes,clock):
    """Exchange-session anchored, complete buckets only. Never partial HTF bars."""
    groups={}
    for r in candles:
        at=stamp(r['timestamp']);local=at.astimezone(IST)
        origin=local.replace(hour=9,minute=15,second=0,microsecond=0)
        offset=int((local-origin).total_seconds()/60)
        if not 0<=offset<375:continue
        start=origin+timedelta(minutes=(offset//minutes)*minutes)
        if start+timedelta(minutes=minutes)>clock or start.date()!=local.date():continue
        groups.setdefault(start,[]).append(r)
    out=[]
    for start,rs in sorted(groups.items()):
        rs=sorted(rs,key=lambda x:x['timestamp'])
        expected=[int((start+timedelta(minutes=i)).timestamp()) for i in range(minutes)]
        if [r['timestamp'] for r in rs]!=expected:continue
        out.append({'timestamp':int(start.timestamp()),'closed_at':(start+timedelta(minutes=minutes)).isoformat(),
                    'open':rs[0]['open'],'high':max(r['high'] for r in rs),'low':min(r['low'] for r in rs),
                    'close':rs[-1]['close'],'volume':sum(r['volume'] for r in rs)})
    return out

def ema(values,period):
    if len(values)<period:return None
    avg=sum(values[:period])/period
    for x in values[period:]:avg+=(x-avg)*2/(period+1)
    return avg

def indicators(candles,clock):
    result={}
    for minutes in (1,5,15,60):
        bars=aggregate(candles,minutes,clock);close=[r['close'] for r in bars]
        e20,e50=ema(close,20),ema(close,50)
        result[str(minutes)]={'bars':len(bars),'ema20':e20,'ema50':e50,
            'bias':'UNAVAILABLE' if e20 is None or e50 is None else 'BULLISH' if e20>e50 else 'BEARISH' if e20<e50 else 'SIDEWAYS'}
    return result

def chain_analysis(record,clock,options=None):
    if not record:return {'status':'NOT_FETCHED','contracts':[],'pcr_oi':None,'pcr_volume':None,'pcr_history':None,'iv_percentile':None}
    m=record.get('metadata',{});raw=record.get('raw',{}).get('data',{});oc=raw.get('oc',{})
    state=freshness(m.get('exchange_at'),m.get('fetched_at'),clock,10)
    expired=not m.get('expiry') or m['expiry']<clock.astimezone(IST).date().isoformat()
    if expired:state={'state':'EXPIRED','age_seconds':None}
    rows=[];sums={s:{k:0 for k in ('oi','volume')} for s in ('CE','PE')};complete=True
    for strike,pair in oc.items() if isinstance(oc,dict) else []:
        strike_value=number(strike)
        if strike_value is None or strike_value<=0:continue
        for key,side in (('ce','CE'),('pe','PE')):
            d=pair.get(key,{}) if isinstance(pair,dict) else {}
            if not d:complete=False;continue
            vals={k:number(d.get(k)) for k in ('last_price','oi','previous_oi','volume','top_bid_price','top_ask_price','top_bid_quantity','top_ask_quantity')}
            for k in ('oi','volume'):
                if vals[k] is None or vals[k]<0:complete=False
                else:sums[side][k]+=vals[k]
            bid,ask=vals['top_bid_price'],vals['top_ask_price']
            valid=bid is not None and ask is not None and 0<bid<=ask
            spread=100*(ask-bid)/((ask+bid)/2) if valid else None
            sid=str(d.get('security_id',''));identity=(options or {}).get(sid)
            verified=bool(identity and identity.get('expiry')==m.get('expiry') and identity.get('strike')==strike_value and identity.get('side')==side)
            greeks=d.get('greeks') or {}
            rows.append({'security_id':sid,'expiry':m.get('expiry'),'strike':strike_value,'side':side,
                'last_price':vals['last_price'],'bid':bid,'ask':ask,'spread_pct':spread,'oi':vals['oi'],'volume':vals['volume'],
                'delta_oi':vals['oi']-vals['previous_oi'] if vals['oi'] is not None and vals['previous_oi'] is not None else None,
                'iv':number(d.get('implied_volatility')) if state['state']=='FRESH' else None,
                'greeks':{k:number(greeks.get(k)) for k in ('delta','gamma','theta','vega')} if state['state']=='FRESH' else None,
                'liquidity':'UNAVAILABLE' if state['state']!='FRESH' else 'INVALID_QUOTE' if not valid else 'SPREAD_TOO_WIDE' if spread>1 else 'CHECK_SIZE_AND_ACTIVITY',
                'lot_size':identity.get('lot_size') if verified else None,'identity_verified':verified,
                'exchange_at':m.get('exchange_at'),'received_at':m.get('fetched_at'),'freshness':state['state']})
    ratio=lambda k: sums['PE'][k]/sums['CE'][k] if complete and sums['CE'][k]>0 else None
    return {'status':state['state'],'freshness':state,'index':m.get('index'),'expiry':m.get('expiry'),
            'received_at':m.get('fetched_at'),'exchange_at':m.get('exchange_at'),'source':'Dhan optionchain',
            'spot':number(raw.get('last_price')),'contracts':sorted(rows,key=lambda r:(r['strike'],r['side'])),
            'pcr_oi':ratio('oi'),'pcr_volume':ratio('volume'),'pcr_basis':'All returned strikes of this expiry; stale snapshot context only when not FRESH',
            'pcr_history':None,'pcr_history_reason':'Continuous timestamped cadence is not established',
            'iv_percentile':None,'iv_percentile_reason':'Verified comparable IV history required',
            'greeks_reason':None if state['state']=='FRESH' else 'Provider exchange quote timestamp missing, stale or expired; Greeks and IV hidden'}

class RuleConfig(Model):
    breakout_buffer_pct: FiniteFloat = Field(default=0.05,ge=0,le=2)
    retest_tolerance_pct: FiniteFloat = Field(default=0.05,ge=0,le=2)
    max_retest_bars: int = Field(default=6,ge=1,le=12)
    min_breadth_fraction: FiniteFloat = Field(default=0.6,ge=.5,le=1)
    max_spread_pct: FiniteFloat = Field(default=1,gt=0,le=10)
    min_volume: int = Field(default=100,ge=1)
    min_oi: int = Field(default=100,ge=1)
    min_room_r: FiniteFloat = Field(default=1.5,ge=1,le=10)
    expiry_day_allowed: bool = False

def setup_rules(candles,clock,setup,config=None,contract=None,breadth=None,context_fresh=False,room_r=None,indicator_view=None):
    cfg=config or RuleConfig();reasons=[];missing=[];wait=[];reject=[]
    closed=[r for r in candles if stamp(r['timestamp'])+timedelta(minutes=1)<=clock]
    today=[r for r in closed if stamp(r['timestamp']).astimezone(IST).date()==clock.astimezone(IST).date()]
    five=aggregate(today,5,clock);ind=indicator_view if indicator_view is not None else indicators(closed,clock);bias=ind['60']['bias'];direction=None
    if bias=='UNAVAILABLE':missing.append('HTF_WARMUP_REQUIRED')
    elif bias=='SIDEWAYS':wait.append('HTF_SIDEWAYS')
    elif ind['15']['bias']!=bias:
        (missing if ind['15']['bias']=='UNAVAILABLE' else reject).append('HTF_CONFLICT' if ind['15']['bias']!='UNAVAILABLE' else 'MTF_WARMUP_REQUIRED')
    else:reasons.append('HTF_ALIGNED');direction='CE' if bias=='BULLISH' else 'PE'
    level=invalid=None
    if setup=='opening_range':
        opening=[r for r in today if '09:15'<=stamp(r['timestamp']).astimezone(IST).strftime('%H:%M')<'09:30']
        expected={int(clock.astimezone(IST).replace(hour=9,minute=15,second=0,microsecond=0).timestamp())+i*60 for i in range(15)}
        if {r['timestamp'] for r in opening}!=expected:missing.append('OPENING_RANGE_INCOMPLETE')
        elif len(five)<5:wait.append('RETEST_PENDING')
        elif direction:
            up=direction=='CE';level=max(r['high'] for r in opening) if up else min(r['low'] for r in opening)
            post=[r for r in five if stamp(r['timestamp']).astimezone(IST).strftime('%H:%M')>='09:30']
            breaks=[i for i,r in enumerate(post[:-1]) if (r['close']>level*(1+cfg.breakout_buffer_pct/100) if up else r['close']<level*(1-cfg.breakout_buffer_pct/100))]
            if not breaks:wait.append('RANGE_BREAK_PENDING')
            else:
                i=breaks[0];reasons.append('RANGE_BREAK_OK');middle=post[i+1:-1];last=post[-1]
                holds=[r for r in middle if (level*(1-cfg.retest_tolerance_pct/100)<=r['low']<=level*(1+cfg.retest_tolerance_pct/100) and r['close']>=level if up else level*(1-cfg.retest_tolerance_pct/100)<=r['high']<=level*(1+cfg.retest_tolerance_pct/100) and r['close']<=level)]
                if len(post)-i-1>cfg.max_retest_bars:reject.append('LATE_ENTRY')
                elif not holds:wait.append('RETEST_PENDING')
                else:
                    retest=holds[-1];invalid=retest['low'] if up else retest['high']
                    if (last['close']>retest['high'] if up else last['close']<retest['low']):reasons.extend(['RETEST_HOLD_OK','FRESH_TRIGGER_OK'])
                    else:wait.append('FRESH_TRIGGER_PENDING')
    else:
        fifteen=aggregate(closed,15,clock);avg=ema([r['close'] for r in fifteen[:-1]],20)
        if avg is None or len(five)<2:missing.append('PULLBACK_WARMUP_REQUIRED')
        elif direction:
            bar=fifteen[-1];up=direction=='CE';level=avg;tol=avg*cfg.retest_tolerance_pct/100
            touch=bar['low']<=avg+tol and bar['high']>=avg-tol
            hold=bar['close']>=avg if up else bar['close']<=avg
            if not touch:wait.append('PULLBACK_PENDING')
            elif not hold:reject.append('STRUCTURE_BROKEN')
            elif (five[-1]['close']>five[-2]['high'] if up else five[-1]['close']<five[-2]['low']):
                reasons.extend(['PULLBACK_HOLD_OK','FRESH_TRIGGER_OK']);invalid=bar['low'] if up else bar['high']
            else:wait.append('FRESH_TRIGGER_PENDING')
    if not closed:missing.append('CANDLES_MISSING')
    else:
        f=freshness(closed[-1]['closed_at'],closed[-1]['received_at'],clock,90)
        if f['state']!='FRESH':missing.append('CANDLE_'+f['state'])
    local=clock.astimezone(IST)
    if not (local.weekday()<5 and '09:30'<=local.strftime('%H:%M')<'15:00'):reject.append('OUTSIDE_ENTRY_WINDOW')
    if not context_fresh:missing.append('EVENT_CONTEXT_UNAVAILABLE')
    if breadth is None:missing.append('BREADTH_UNAVAILABLE')
    elif direction and (breadth if direction=='CE' else 1-breadth)<cfg.min_breadth_fraction:reject.append('PARTICIPATION_WEAK')
    if room_r is None:missing.append('ROOM_TO_LEVEL_UNAVAILABLE')
    elif room_r<cfg.min_room_r:reject.append('RESISTANCE_TOO_CLOSE')
    if not contract:missing.append('OPTION_QUOTE_UNAVAILABLE')
    else:
        if not contract.get('identity_verified') or not contract.get('lot_size'):missing.append('CONTRACT_UNVERIFIED')
        if contract.get('freshness')!='FRESH':missing.append('OPTION_TIME_UNVERIFIED')
        if direction and contract.get('side')!=direction:reject.append('OPTION_SIDE_CONFLICT')
        spread=contract.get('spread_pct')
        if spread is None:missing.append('BID_ASK_UNAVAILABLE')
        elif spread>cfg.max_spread_pct:reject.append('SPREAD_TOO_WIDE')
        if (contract.get('volume') or 0)<cfg.min_volume or (contract.get('oi') or 0)<cfg.min_oi:reject.append('LIQUIDITY_TOO_LOW')
        if contract.get('expiry')==local.date().isoformat() and not cfg.expiry_day_allowed:reject.append('EXPIRY_DAY_BLOCKED')
    state='DATA_UNAVAILABLE' if missing else 'NO_TRADE' if reject else 'WAIT' if wait else 'CONFIRM'
    return {'state':state,'setup':setup,'rule_version':RULE_VERSION,'config':cfg.model_dump(),
            'clock':clock.isoformat(),'side':direction,'reason_codes':list(dict.fromkeys(missing+reject+wait+reasons)),
            'missing':missing,'failed':reject,'pending':wait,'passed':reasons,'reference_level':level,'underlying_invalidation':invalid,
            'indicators':ind,'execution':'NO_BROKER_EXECUTION','validation':'UNVALIDATED_RESEARCH_RULES'}

class RiskInput(Model):
    entry: FiniteFloat = Field(gt=0)
    stop: FiniteFloat = Field(gt=0)
    risk_rupees: FiniteFloat = Field(gt=0,le=1000000)
    capital: FiniteFloat = Field(gt=0,le=100000000)
    daily_loss_cap: FiniteFloat = Field(gt=0,le=1000000)
    realised_loss: FiniteFloat = Field(ge=0,le=10000000)
    # Total reserve for all concurrently open trades, supplied explicitly.
    open_risk: FiniteFloat = Field(ge=0,le=10000000)
    lot_size: int = Field(gt=0,le=100000)
    lot_source: str = Field(min_length=3,max_length=200)
    lot_verified_on: date
    brokerage_roundtrip: FiniteFloat = Field(ge=0)
    exchange_roundtrip_per_lot: FiniteFloat = Field(ge=0)
    stt_exit_rate: FiniteFloat = Field(ge=0,le=.1)
    sebi_turnover_rate: FiniteFloat = Field(ge=0,le=.01)
    gst_rate: FiniteFloat = Field(ge=0,le=1)
    stamp_entry_rate: FiniteFloat = Field(ge=0,le=.01)
    slippage_per_unit: FiniteFloat = Field(ge=0)
    spread_per_unit: FiniteFloat = Field(ge=0)
    costs_effective_on: date
    costs_source: str = Field(min_length=3,max_length=300)
    expiry: date
    expiry_day_allowed: bool = False
    @model_validator(mode='after')
    def stop_below_entry(self):
        if self.stop>=self.entry:raise ValueError('Long-option stop must be below entry')
        return self

def risk_plan(p,clock=None):
    today=(clock or datetime.now(timezone.utc)).astimezone(IST).date();reasons=[]
    if p.lot_verified_on!=today:reasons.append('LOT_METADATA_NOT_CURRENT')
    if p.costs_effective_on>today:reasons.append('COSTS_NOT_EFFECTIVE')
    if p.expiry<today:reasons.append('CONTRACT_EXPIRED')
    if p.expiry==today and not p.expiry_day_allowed:reasons.append('EXPIRY_DAY_BLOCKED')
    budget=max(0,min(p.risk_rupees,p.daily_loss_cap-p.realised_loss-p.open_risk))
    turnover=(p.entry+p.stop)*p.lot_size
    transaction=p.exchange_roundtrip_per_lot+turnover*p.sebi_turnover_rate
    costs=(transaction*(1+p.gst_rate)+p.stop*p.lot_size*p.stt_exit_rate+
           p.entry*p.lot_size*p.stamp_entry_rate+(p.slippage_per_unit+p.spread_per_unit)*p.lot_size)
    fixed=p.brokerage_roundtrip*(1+p.gst_rate)
    per_lot=(p.entry-p.stop)*p.lot_size+costs
    lots=max(0,min(floor(max(0,budget-fixed)/per_lot),floor(max(0,p.capital-fixed)/(p.entry*p.lot_size+costs))))
    if not lots:reasons.append('RISK_BUDGET_INSUFFICIENT')
    if reasons:lots=0
    return {'state':'NO_TRADE' if reasons else 'SIZED','lots':lots,'quantity':lots*p.lot_size,
            'risk_budget_available':budget,'planned_stop_loss':round(lots*per_lot+(fixed if lots else 0),2),
            'estimated_costs':round(lots*costs+(fixed if lots else 0),2),
            'premium_paid':round(lots*p.lot_size*p.entry,2),'reason_codes':reasons,
            'assumptions':p.model_dump(mode='json'),'warning':'Planning estimate; stop fills may gap. Premium at risk can exceed stop budget. Charges are explicit user inputs, not a verified tariff feed.'}

class PaperNote(Model):
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    symbol: Literal['nifty','banknifty']
    decision: Literal['WAIT','NO_TRADE','CE_OBSERVED','PE_OBSERVED','EXIT_REVIEW']
    note: str = Field(min_length=1,max_length=2000)
    deviations: str = Field(default='',max_length=2000)
    setup: Literal['opening_range','trend_pullback']
    # No client-supplied fills or P&L can masquerade as an executed paper trade.

class ReplayInput(Model):
    symbol: Literal['nifty','banknifty']
    start: date
    end: date
    clock: datetime
    setup: Literal['opening_range','trend_pullback'] = 'opening_range'
    config: RuleConfig = Field(default_factory=RuleConfig)
    _aware=field_validator('clock')(aware)
    @model_validator(mode='after')
    def dates(self):
        if not self.start<self.end or (self.end-self.start).days>7:raise ValueError('Replay range must be 1–7 days')
        if self.end>datetime.now(IST).date()+timedelta(days=1):raise ValueError('Future replay range')
        if not self.start<=self.clock.astimezone(IST).date()<self.end:raise ValueError('Clock must be inside the selected replay dates')
        return self

def replay_report(store,p):
    if p.clock>datetime.now(timezone.utc):raise ValueError('Replay clock must not be in the future')
    loaded=saved_candles(store,p.symbol,p.clock)
    # Filter BEFORE the engine sees anything: later bars cannot leak through HTF aggregation.
    visible=[r for r in loaded['candles'] if stamp(r['timestamp'])+timedelta(minutes=1)<=p.clock]
    timeline=aggregate([r for r in visible if p.start<=stamp(r['timestamp']).astimezone(IST).date()<p.end],5,p.clock)
    # Precompute prefix-only indicator states once, then expose just the state
    # whose close time is <= each event. No future state enters setup_rules.
    from bisect import bisect_right
    indicator_cache={}
    for minutes in (1,5,15,60):
        series=aggregate(visible,minutes,p.clock);states=[];times=[];sums={20:0.,50:0.};averages={20:None,50:None}
        for i,bar in enumerate(series):
            for period in (20,50):
                if i<period:sums[period]+=bar['close']
                if i==period-1:averages[period]=sums[period]/period
                elif i>=period:averages[period]+=(bar['close']-averages[period])*2/(period+1)
            e20,e50=averages[20],averages[50]
            states.append({'bars':i+1,'ema20':e20,'ema50':e50,'bias':'UNAVAILABLE' if e20 is None or e50 is None else 'BULLISH' if e20>e50 else 'BEARISH' if e20<e50 else 'SIDEWAYS'})
            times.append(stamp(bar['closed_at']))
        indicator_cache[str(minutes)]=(times,states)
    def known_indicators(at):
        view={}
        for key,(times,states) in indicator_cache.items():
            index=bisect_right(times,at)-1
            view[key]=states[index] if index>=0 else {'bars':0,'ema20':None,'ema50':None,'bias':'UNAVAILABLE'}
        return view
    events=[]
    for bar in timeline[-600:]:
        at=stamp(bar['closed_at']);known=[r for r in visible if stamp(r['timestamp'])+timedelta(minutes=1)<=at]
        # Historical receive times are not replaced with the event clock.
        # No unavailable chain/context observations are reconstructed.
        result=setup_rules(known,at,p.setup,p.config,indicator_view=known_indicators(at))
        events.append({'clock':at.isoformat(),'close':bar['close'],'state':result['state'],'reason_codes':result['reason_codes']})
    return {'clock':p.clock.isoformat(),'symbol':p.symbol,'events':events,'rule_version':RULE_VERSION,
            'scope':loaded['read_scope'],'conflicting_minutes':loaded['conflicting_minutes'],
            'option_pnl':None,'metrics':None,'status':'UNDERLYING_REPLAY_ONLY',
            'gates':['EXACT_CONTRACT_HISTORY_UNVERIFIED','HISTORICAL_BID_ASK_UNAVAILABLE','EXECUTION_COST_MODEL_REQUIRED','EVENT_CALENDAR_UNVERIFIED'],
            'split':'No performance claims or walk-forward statistics until contract/execution gates pass',
            'latest':setup_rules(visible,p.clock,p.setup,p.config)}

def workspace(store,symbol,clock=None,options=None):
    clock=clock or datetime.now(timezone.utc);loaded=saved_candles(store,symbol,clock)
    candles=loaded['candles'];latest=candles[-1] if candles else None
    records=store.rows('dhan_chain',40)
    chosen=next((r for r in records if r['data'].get('metadata',{}).get('index')==symbol),None)
    chain=chain_analysis(chosen['data'] if chosen else None,clock,options)
    session_rows={}
    for row in candles:
        day=stamp(row['timestamp']).astimezone(IST).date().isoformat()
        session_rows.setdefault(day,0);session_rows[day]+=1
    session_quality=[{'date':day,'observed_minutes':count,'missing_regular_grid_minutes':max(0,375-count),
                      'calendar_certified':False} for day,count in sorted(session_rows.items())]
    return {'symbol':symbol,'server_time':clock.isoformat(),'mode':'READ_ONLY_RESEARCH','rule_version':RULE_VERSION,
            'session_quality':session_quality,
            'policies':POLICY,'history':loaded,'indicators':indicators(candles,clock),
            'latest_candle':latest,'candle_freshness':freshness(latest['closed_at'],latest['received_at'],clock,90) if latest else {'state':'MISSING'},
            'chain':chain,'setups':[setup_rules(candles,clock,s) for s in ('opening_range','trend_pullback')],
            'availability':{'breadth':'NOT_CONNECTED','events':'NOT_CONNECTED','futures_vwap':'NOT_VERIFIED',
                            'continuous_feed':'NOT_CONNECTED','iv_percentile':'HISTORY_UNVERIFIED','pcr_history':'CADENCE_UNVERIFIED'},
            'validation':{'rule_state':'UNVALIDATED','paper_trade_minimum':50,'multiple_regimes_required':True,
                          'option_backtest_ready':False,'live_orders':False}}
