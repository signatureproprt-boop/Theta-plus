"""Deterministic research rules shared by current analysis and replay.

Unvalidated hypotheses, never calibrated trade probabilities. All observations
are as-of and option fills are tied to a fixed expiry/security/strike/side.
"""
from bisect import bisect_right
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import math
import hashlib
import json
from models import Candle, Snapshot, Contract, Dataset, Experiment

IST=ZoneInfo('Asia/Kolkata')
VERSION='theta-research-2.3.5'

def session(t):
    return t.astimezone(IST).date()

def session_open(t):
    local=t.astimezone(IST)
    return local.weekday()<5 and (9,15)<=(local.hour,local.minute)<(15,30)

def same_contract(a,b):
    return (a.security_id,a.expiry,a.strike,a.side)==(b.security_id,b.expiry,b.strike,b.side)

def bars5(candles):
    groups={}
    for c in candles:
        if not session_open(c.timestamp):
            continue
        key=c.timestamp.replace(minute=c.timestamp.minute//5*5,second=0,microsecond=0)
        groups.setdefault(key,[]).append(c)
    bars=[]
    for t,rows in sorted(groups.items()):
        rows=sorted(rows,key=lambda x:x.timestamp)
        if len(rows)!=5 or [x.timestamp for x in rows] != [t+timedelta(minutes=i) for i in range(5)]:
            continue
        bars.append({'start':t,'end':t+timedelta(minutes=5),'open':rows[0].open,
                     'high':max(x.high for x in rows),'low':min(x.low for x in rows),'close':rows[-1].close})
    return bars

def timeframe_trend(bars, at, minutes=15):
    """As-of structure from complete, contiguous bars; no partial session bars."""
    base={'direction':'UNKNOWN','timeframe':f'{minutes}m' if minutes else 'daily',
          'bars':0,'reason':'Insufficient completed candles','as_of':None,'invalidation':None}
    if minutes:
        blocks={}
        for b in bars:
            if b['end']>at or session(b['start'])!=session(at):continue
            local=b['start'].astimezone(IST)
            offset=(local.hour*60+local.minute)-(9*60+15)
            if offset<0:continue
            start=local-timedelta(minutes=offset%minutes)
            blocks.setdefault(start,[]).append(b)
        complete=[]
        for start,rows in sorted(blocks.items()):
            rows=sorted(rows,key=lambda b:b['start'])
            if len(rows)!=minutes//5 or any(b['start']!=start+timedelta(minutes=5*i) for i,b in enumerate(rows)):
                continue
            complete.append({'start':start,'end':start+timedelta(minutes=minutes),
                             'open':rows[0]['open'],'close':rows[-1]['close'],
                             'high':max(b['high'] for b in rows),'low':min(b['low'] for b in rows)})
        rows=complete
        if not rows or at-rows[-1]['end']>timedelta(minutes=minutes+6):
            return {**base,'reason':'Latest completed timeframe candle is stale or missing'}
        recent=rows[-4:]
        if len(recent)<4 or any(b['start']!=a['end'] for a,b in zip(recent,recent[1:])):
            return {**base,'bars':len(rows),'reason':'Need four contiguous completed timeframe candles'}
    else:
        groups={}
        for b in bars:
            if b['end']<=at and session(b['start'])<session(at):groups.setdefault(session(b['start']),[]).append(b)
        rows=[]
        for day,items in sorted(groups.items()):
            items=sorted(items,key=lambda b:b['start'])
            # A full NSE day has 75 five-minute bars, from 09:15 through 15:30 IST.
            if len(items)!=75 or items[0]['start'].astimezone(IST).time().strftime('%H:%M')!='09:15' or any(b['start']!=a['end'] for a,b in zip(items,items[1:])):
                continue
            rows.append({'start':items[0]['start'],'end':items[-1]['end'],'open':items[0]['open'],
                         'close':items[-1]['close'],'high':max(b['high'] for b in items),
                         'low':min(b['low'] for b in items)})
        if len(rows)<4:return {**base,'bars':len(rows),'reason':'Need four prior complete trading sessions; live connector supplies only today'}
        recent=rows[-4:]
    prior,last=recent[:2],recent[2:]
    high0=max(b['high'] for b in prior);high1=max(b['high'] for b in last)
    low0=min(b['low'] for b in prior);low1=min(b['low'] for b in last)
    direction='RANGE';invalidation=None
    if high1>high0 and low1>low0:direction='BULLISH';invalidation=low1
    elif high1<high0 and low1<low0:direction='BEARISH';invalidation=high1
    return {**base,'direction':direction,'bars':len(rows),'as_of':rows[-1]['end'].isoformat(),
            'invalidation':invalidation,'reason':'Last two completed bars versus preceding two; historical research only'}

def chart_evidence(bars,at,strategy='structure'):
    closed=[b for b in bars if b['end']<=at and session(b['start'])==session(at)][-60:]
    base={'direction':'UNKNOWN','strategy':strategy,'bars':len(closed),'reason':'Need completed, contiguous 5-minute bars','efficiency':None}
    if len(closed)<5:
        return base
    if at-closed[-1]['end']>timedelta(minutes=6):
        return {**base,'reason':'Latest completed candle is stale'}
    if any(b['start'] != a['end'] for a,b in zip(closed[-5:],closed[-4:])):
        return {**base,'reason':'Gap in recent candle history'}
    closes=[b['close'] for b in closed]
    travel=sum(abs(b-a) for a,b in zip(closes[-6:],closes[-5:])) if len(closes)>=6 else sum(abs(b-a) for a,b in zip(closes,closes[1:]))
    efficiency=abs(closes[-1]-closes[max(0,len(closes)-6)])/travel if travel else 0
    direction='RANGE'; invalidation=None
    if strategy=='opening_range':
        opening=[b for b in closed if (9,15)<=(b['start'].astimezone(IST).hour,b['start'].astimezone(IST).minute)<(9,30)]
        if len(opening)!=3:
            return {**base,'reason':'Opening-range candles missing'}
        high=max(b['high'] for b in opening);low=min(b['low'] for b in opening)
        if closes[-1]>high: direction='BULLISH';invalidation=high
        elif closes[-1]<low: direction='BEARISH';invalidation=low
        reason='Completed close relative to 09:15–09:30 IST range; breakout hypothesis'
    elif strategy=='ema':
        if len(closed)<21:
            return {**base,'reason':'EMA 9/21 requires at least 21 completed bars'}
        def ema(n):
            value=closes[0]
            for c in closes[1:]: value+=2/(n+1)*(c-value)
            return value
        fast,slow=ema(9),ema(21)
        if closes[-1]>fast>slow: direction='BULLISH';invalidation=slow
        elif closes[-1]<fast<slow: direction='BEARISH';invalidation=slow
        reason='Completed price and EMA 9/21 alignment; session-local warmup'
    else:
        # Last two bars versus preceding two: explicit and known at decision time.
        prior,recent=closed[-4:-2],closed[-2:]
        if min(b['low'] for b in recent)>min(b['low'] for b in prior) and max(b['high'] for b in recent)>max(b['high'] for b in prior):
            direction='BULLISH';invalidation=min(b['low'] for b in recent)
        elif min(b['low'] for b in recent)<min(b['low'] for b in prior) and max(b['high'] for b in recent)<max(b['high'] for b in prior):
            direction='BEARISH';invalidation=max(b['high'] for b in recent)
        reason='Recent two completed bars versus previous two; not a predictive certainty'
    return {**base,'direction':direction,'efficiency':round(efficiency,3),'reason':reason,'invalidation':invalidation}

def usable(c,at,max_spread=2):
    if c.bid<=0 or c.ask<c.bid or c.ask<=0: return False
    if c.quote_at is None or not 0<=(at-c.quote_at).total_seconds()<=30: return False
    if c.expiry<session(at): return False
    return (c.ask-c.bid)/c.ask*100<=max_spread

def option_evidence(current,history):
    base={'direction':'UNKNOWN','reason':'Need a same-session snapshot 5–10 minutes earlier',
          'call_oi_delta':None,'put_oi_delta':None,'matched_contracts':0,'baseline_at':None}
    choices=[s for s in history if session(s.timestamp)==session(current.timestamp)
             and 300<=(current.timestamp-s.timestamp).total_seconds()<=600 and s.source==current.source]
    if not choices:return base
    prev=max(choices,key=lambda s:s.timestamp)
    expiry=min((c.expiry for c in current.contracts if c.expiry>=session(current.timestamp)),default=None)
    strikes=sorted({c.strike for c in current.contracts if c.expiry==expiry},key=lambda k:abs(k-current.spot))[:5]
    old={c.security_id:c for c in prev.contracts}
    matched=[]
    for c in current.contracts:
        p=old.get(c.security_id)
        if c.expiry==expiry and c.strike in strikes and p and same_contract(c,p) and usable(c,current.timestamp,5) and usable(p,prev.timestamp,5):
            matched.append((c,p))
    paired=set(c.strike for c,p in matched if c.side=='CE') & set(c.strike for c,p in matched if c.side=='PE')
    matched=[(c,p) for c,p in matched if c.strike in paired]
    if len(paired)<3:return {**base,'reason':'Need fresh paired CE/PE quotes at 3 or more fixed strikes'}
    deltas={side:sum(c.oi-p.oi for c,p in matched if c.side==side) for side in ('CE','PE')}
    moves={side:sum(((c.bid+c.ask)-(p.bid+p.ask))/2 for c,p in matched if c.side==side) for side in ('CE','PE')}
    direction='MIXED'
    if deltas['PE']>0 and deltas['CE']<0 and moves['CE']>0 and moves['PE']<0:direction='BULLISH'
    elif deltas['CE']>0 and deltas['PE']<0 and moves['PE']>0 and moves['CE']<0:direction='BEARISH'
    return {**base,'direction':direction,'call_oi_delta':deltas['CE'],'put_oi_delta':deltas['PE'],
            'matched_contracts':len(matched),'baseline_at':prev.timestamp.isoformat(),
            'reason':'Fixed-strike ΔOI + midpoint pattern hypothesis; not proof of buying/writing or independent of index moves'}

def decision(chart,options,route='combined',trend=None):
    c,o=chart['direction'],options['direction']
    directional=('BULLISH','BEARISH')
    if route=='chart':return c if c in directional else 'WAIT'
    if route=='options':return o if o in directional else 'WAIT'
    if route=='trend_combined':
        if not trend:return 'WAIT'
        higher=(trend['intraday_15m']['direction'],trend['prior_daily']['direction'])
        return c if c in directional and c==o and all(x==c for x in higher) else 'WAIT'
    return c if c in directional and c==o else 'WAIT'

def size_plan(c,capital,budget,stop,costs,slip,at,max_spread=2):
    reasons=[]
    if not usable(c,at,max_spread):reasons.append('Missing/stale quote, invalid spread or expired contract')
    if not c.lot_size:reasons.append('Verified contract lot size is missing')
    if not 0<stop<c.ask:reasons.append('Stop quote must be positive and below entry ask')
    if budget>capital:reasons.append('Risk budget exceeds capital')
    if reasons:return {'allowed':False,'reasons':reasons,'lots':0}
    unit=c.lot_size
    # Allow slippage on both entry and exit; spread is in ask-to-stop distance.
    per_lot_risk=(c.ask+2*slip-stop)*unit+costs
    per_lot_cash=(c.ask+slip)*unit+costs
    lots=max(0,math.floor(min(budget/per_lot_risk,capital/per_lot_cash)))
    # Displayed depth is not guaranteed, but never assume unlimited fills.
    lots=min(lots,c.ask_qty//unit)
    if lots==0:reasons.append('One lot does not fit budget, capital or displayed ask quantity')
    return {'allowed':bool(lots),'reasons':reasons,'lots':lots,'quantity':lots*unit,
            'entry':c.ask+slip,'stop':stop,'planned_loss':round(lots*per_lot_risk,2),
            'premium_outlay':round(lots*(c.ask+slip)*unit,2),'full_premium_risk':round(lots*per_lot_cash,2),
            'costs':round(lots*costs,2),'slippage_per_unit':slip,
            'note':'Planned stop loss is not guaranteed; quotes/depth may change. Long premium can be lost in full.'}

def replay(dataset:Dataset,exp:Experiment):
    snapshots=sorted(dataset.snapshots,key=lambda s:s.timestamp)
    bars=bars5(dataset.candles);ends=[b['end'] for b in bars]
    bar_index=0;history=[];active=None;pending=None;trades=[];blocked=0;counts={};day_pnl={}
    assessed=0;eligible=0;trend_ready=0;exposure_unresolved=False
    def finish(s,c,reason):
        nonlocal active,exposure_unresolved
        if c is None:
            exposure_unresolved=True
            trades.append({**active,'exit_at':None,'exit':None,'net_pnl':None,'reason':reason})
        else:
            price=max(0,c.bid-exp.slippage_per_unit)
            pnl=round((price-active['entry'])*active['quantity']-active['costs'],2)
            trades.append({**active,'exit_at':s.timestamp.isoformat(),'exit':price,'net_pnl':pnl,'reason':reason})
            day_pnl[active['session']]=day_pnl.get(active['session'],0)+pnl
        active=None
    for s in snapshots:
        if not session_open(s.timestamp):continue
        day=str(session(s.timestamp))
        if active:
            c=next((c for c in s.contracts if c.security_id==active['security_id'] and str(c.expiry)==active['expiry'] and c.side==active['side'] and c.strike==active['strike'] and s.source==active['source']),None)
            entry_at=datetime.fromisoformat(active['entry_at'])
            if day!=active['session']:
                finish(s,None,'UNRESOLVED_SESSION_END')
            elif (s.timestamp-datetime.fromisoformat(active['last_seen'])).total_seconds()>120:
                finish(s,None,'UNRESOLVED_DATA_GAP')
            elif c and usable(c,s.timestamp,100) and c.bid_qty>=active['quantity']:
                active['last_seen']=s.timestamp.isoformat()
                if c.bid<=active['stop']:finish(s,c,'STOP_OBSERVED')
                elif c.bid>=active['target']:finish(s,c,'TARGET_OBSERVED')
                elif s.timestamp-entry_at>=timedelta(minutes=exp.max_hold_minutes):finish(s,c,'TIME_EXIT')
                elif s.timestamp.astimezone(IST).time().hour==15 and s.timestamp.astimezone(IST).minute>=20:finish(s,c,'SESSION_EXIT')
        if active:
            history.append(s);continue
        if pending:
            signal_s,side=pending;pending=None
            if session(signal_s.timestamp)==session(s.timestamp) and 0<(s.timestamp-signal_s.timestamp).total_seconds()<=60 and s.source==signal_s.source:
                # Contract chosen at the signal, then filled at the next observation.
                choices=[c for c in signal_s.contracts if c.side==side and usable(c,signal_s.timestamp,exp.max_spread_pct) and c.lot_size]
                choices.sort(key=lambda c:(c.expiry,abs(c.strike-signal_s.spot),(c.ask-c.bid)/c.ask))
                pick=choices[0] if choices else None
                c=next((x for x in s.contracts if pick and same_contract(x,pick) and x.lot_size==pick.lot_size),None)
                if c:
                    remaining=min(exp.risk_per_trade,max(0,2*exp.risk_per_trade+day_pnl.get(day,0)))
                    plan=size_plan(c,max(0,exp.capital+sum(t['net_pnl'] or 0 for t in trades)),remaining,c.ask*(1-exp.stop_pct/100),exp.costs_per_lot,exp.slippage_per_unit,s.timestamp,exp.max_spread_pct)
                    if plan['allowed']:
                        active={**plan,'source':s.source,'security_id':c.security_id,'expiry':str(c.expiry),'strike':c.strike,'side':c.side,'session':day,'signal_at':signal_s.timestamp.isoformat(),'entry_at':s.timestamp.isoformat(),'last_seen':s.timestamp.isoformat(),'target':plan['entry']+(plan['entry']-plan['stop'])*exp.reward_multiple}
                        counts[day]=counts.get(day,0)+1
                    else:blocked+=1
                else:blocked+=1
            else:blocked+=1
        if not active and not exposure_unresolved and counts.get(day,0)<3 and day_pnl.get(day,0)>-2*exp.risk_per_trade and s.timestamp.astimezone(IST).time().hour*60+s.timestamp.astimezone(IST).minute<15*60:
            assessed+=1
            bar_index=bisect_right(ends,s.timestamp)
            c=chart_evidence(bars[max(0,bar_index-60):bar_index],s.timestamp,exp.strategy)
            o=option_evidence(s,history[-150:])
            trend=None
            if exp.route=='trend_combined':
                available=bars[:bar_index]
                trend={'intraday_15m':timeframe_trend(available,s.timestamp,15),
                       'prior_daily':timeframe_trend(available,s.timestamp,0)}
                if all(t['direction'] in ('BULLISH','BEARISH') for t in trend.values()):trend_ready+=1
            d=decision(c,o,exp.route,trend)
            if d in ('BULLISH','BEARISH'):
                eligible+=1;pending=(s,'CE' if d=='BULLISH' else 'PE')
        history.append(s)
    if active:finish(snapshots[-1],None,'UNRESOLVED_DATA_END')
    resolved=[t for t in trades if t['net_pnl'] is not None]
    pnl=[t['net_pnl'] for t in resolved];equity=0;peak=0;drawdown=0;curve=[]
    for t in resolved:
        equity+=t['net_pnl'];peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
        curve.append({'timestamp':t['exit_at'],'net_pnl':round(equity,2)})
    unresolved=len(trades)-len(resolved)
    fingerprint=hashlib.sha256(json.dumps(dataset.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'version':VERSION,'dataset_sha256':fingerprint,'status':'INCOMPLETE' if unresolved else ('NO_ELIGIBLE_TRADES' if not trades else 'COMPLETED'),
            'source':dataset.provenance,'experiment':exp.model_dump(mode='json'),
            'summary':{'entries':len(trades),'resolved':len(resolved),'unresolved':unresolved,'blocked':blocked,
                       'assessed_snapshots':assessed,'eligible_signals':eligible,'trend_ready_snapshots':trend_ready if exp.route=='trend_combined' else None,
                       'resolved_net_pnl':round(sum(pnl),2),'win_rate':round(sum(p>0 for p in pnl)/len(pnl)*100,2) if pnl else None,
                       'resolved_max_drawdown':round(drawdown,2),'average_net':round(sum(pnl)/len(pnl),2) if pnl else None},
            'trades':trades,'equity':curve,
            'limitations':['Research rules are unvalidated; no guaranteed returns.',
                          'Replay uses sampled quotes, not continuous tick paths. Stops may be missed between samples.',
                          'Unknown exits are excluded from resolved-only metrics; these are not total portfolio returns.',
                          'Three entries/session; unresolved exposure blocks all subsequent entries; resolved daily loss limit is twice trade risk.',
                          'The trend_combined route requires aligned 5-minute, 15-minute, prior-day and option evidence; missing data blocks entry.',
                          'Context inputs are displayed separately and are not included in this strategy version.',
                          'No external held-out performance validation has been performed.']}
