"""Manual hypothetical option-buying ledger. No simulated or broker fills inferred."""
from datetime import datetime,timezone
from typing import Literal
from pydantic import Field,FiniteFloat
from fastapi import HTTPException
from models import Model
from research import RiskInput,risk_plan,workspace,RULE_VERSION,estimated_exit_costs
from runtime import IST
from collection import replace

KEY='research/paper-ledger'
class PaperOpen(Model):
    id:str=Field(pattern=r'^[a-f0-9]{32}$')
    symbol:Literal['nifty','banknifty']
    security_id:str=Field(pattern=r'^[0-9]{1,20}$')
    setup:Literal['opening_range','trend_pullback']
    reason:str=Field(min_length=3,max_length=2000)
    deviations:str=Field(default='',max_length=2000)
    risk:RiskInput

class PaperClose(Model):
    exit_premium:FiniteFloat=Field(ge=0)
    reason:str=Field(min_length=3,max_length=2000)
    deviations:str=Field(default='',max_length=2000)

def view(store):
    rows=(store.document(KEY)[0] or {}).get('trades',[])
    closed=[r for r in rows if r['state']=='CLOSED']
    return {'trades':list(reversed(rows)),'recorded_closed_trades':len(closed),'validated_trades':0,
            'hypothetical_net_pnl':round(sum(r['hypothetical_net_pnl'] for r in closed),2),
            'metrics':hypothetical_metrics(closed),
            'qualification':'Manual hypothetical prices. Not broker fills, a verified execution simulation, or proof of strategy accuracy. They do not unlock the 50 verified-paper-trade gate.'}

def open_trade(store,p,master,clock=None):
    clock=clock or datetime.now(timezone.utc);day=clock.astimezone(IST).date()
    source=p.model_dump(mode='json',exclude_none=True)
    prior=next((t for t in (store.document(KEY)[0] or {}).get('trades',[]) if t['id']==p.id),None)
    if prior:
        if prior['input']!=source:raise HTTPException(409,'Paper ID already used with different inputs')
        return prior
    if p.risk.target is None:raise HTTPException(422,'Set an explicit premium target before a paper entry')
    contract=master.get(p.security_id)
    if not contract or contract.get('underlying')!=p.symbol or contract.get('side') not in ('CE','PE'):
        raise HTTPException(422,'Choose an exact option contract from the current master')
    if contract.get('lot_size')!=p.risk.lot_size or contract.get('expiry')!=p.risk.expiry.isoformat():
        raise HTTPException(422,'Risk lot size / expiry does not match the exact master contract')
    snapshot=workspace(store,p.symbol,clock,master,p.security_id)
    setup=next(s for s in snapshot['setups'] if s['setup']==p.setup)
    if setup['state']!='CONFIRM' and not p.deviations.strip():
        raise HTTPException(422,'Record the rule deviation: this setup is not confirmed')
    def add(ledger):
        ledger=ledger or {'trades':[]};rows=ledger['trades']
        old=next((t for t in rows if t['id']==p.id),None)
        if old:
            if old['input']!=source:raise HTTPException(409,'Paper ID already used with different inputs')
            return ledger
        if len(rows)>=500:raise HTTPException(409,'Paper ledger limit reached; export/review before more entries')
        # Compute reserves from the authoritative ledger inside the CAS update.
        reserve=sum(t['risk_plan']['planned_stop_loss'] for t in rows if t['state']=='OPEN')
        realised=sum(max(0,-t['hypothetical_net_pnl']) for t in rows if t['state']=='CLOSED' and t['closed_day']==day.isoformat())
        risk=p.risk.model_copy(update={'open_risk':max(reserve,p.risk.open_risk),'realised_loss':max(realised,p.risk.realised_loss)})
        plan=risk_plan(risk,clock)
        if plan['state']!='SIZED':raise HTTPException(422,'Paper plan blocked: '+', '.join(plan['reason_codes']))
        rows.append({'id':p.id,'input':source,'contract':contract,'state':'OPEN','opened_at':clock.isoformat(),
                     'risk_plan':plan,'entry_premium':p.risk.entry,'stop_premium':p.risk.stop,'target_premium':p.risk.target,'quantity':plan['quantity'],'rule_version':RULE_VERSION,
                     'setup_snapshot':setup,
                     'price_basis':'MANUAL_HYPOTHETICAL','validation_eligible':False,'execution':'NO_BROKER_EXECUTION'})
        return ledger
    ledger=replace(store,KEY,add)
    return next(t for t in ledger['trades'] if t['id']==p.id)

def close_trade(store,tid,p,clock=None):
    clock=clock or datetime.now(timezone.utc)
    def close(ledger):
        if not ledger:raise HTTPException(404,'Paper entry not found')
        trade=next((t for t in ledger['trades'] if t['id']==tid),None)
        if not trade:raise HTTPException(404,'Paper entry not found')
        payload=p.model_dump()
        if trade['state']=='CLOSED':
            if trade['close_input']!=payload:raise HTTPException(409,'Paper exit is immutable')
            return ledger
        if clock<datetime.fromisoformat(trade['opened_at']):raise HTTPException(422,'Exit cannot precede paper entry')
        r=trade['risk_plan']['assumptions'];qty=trade['quantity'];entry=trade['entry_premium'];exit=p.exit_premium
        costs=estimated_exit_costs(RiskInput(**r),qty,exit)
        gross=(exit-entry)*qty
        trade.update(state='CLOSED',close_input=payload,closed_at=clock.isoformat(),closed_day=clock.astimezone(IST).date().isoformat(),
                     gross_hypothetical_pnl=round(gross,2),estimated_costs=round(costs,2),
                     hypothetical_net_pnl=round(gross-costs,2),
                     realised_r=round((gross-costs)/trade['risk_plan']['planned_stop_loss'],4) if trade['risk_plan']['planned_stop_loss'] else None)
        return ledger
    ledger=replace(store,KEY,close)
    return next(t for t in ledger['trades'] if t['id']==tid)


def hypothetical_metrics(closed):
    ordered=sorted(closed,key=lambda t:(t['closed_at'],t['id']))
    net=[t['hypothetical_net_pnl'] for t in ordered]
    equity=peak=drawdown=0.0;rs=[]
    for t,value in zip(ordered,net):
        equity+=value;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
        reserve=t['risk_plan'].get('planned_stop_loss',0)
        if reserve>0:rs.append(value/reserve)
    gains=sum(v for v in net if v>0);losses=-sum(v for v in net if v<0)
    return {'basis':'MANUAL_HYPOTHETICAL_NET_OF_SAVED_COST_ASSUMPTIONS','validated':False,'closed_trades':len(net),
            'win_rate':round(sum(v>0 for v in net)/len(net),4) if net else None,
            'expectancy_rupees':round(sum(net)/len(net),2) if net else None,
            'average_r':round(sum(rs)/len(rs),4) if rs else None,'r_sample_count':len(rs),
            'profit_factor':round(gains/losses,4) if losses else None,
            'max_drawdown_rupees':round(drawdown,2) if net else None}
