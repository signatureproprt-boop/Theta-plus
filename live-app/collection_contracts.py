"""Pure catalogue/quote contracts. Never infer provider availability from metadata."""

OPTION_TYPES={'OPTIDX','OPTSTK','OPTFUT','OPTCUR'}

def chain_universe(items,today):
    """Only option-bearing underlyings; index exchange IDs are not Dhan index IDs."""
    indices={(i['exchange'],i['symbol']):i for i in items if i['instrument']=='INDEX' and not i.get('reason')}
    cash={(i['exchange'],i['security_id']):i for i in items if i['instrument']=='EQUITY' and not i.get('reason')}
    chosen={};unresolved={};expired=0
    for option in items:
        if option['instrument'] not in OPTION_TYPES:continue
        if option.get('expiry') and option['expiry']<today.isoformat():
            expired+=1;continue
        reason=option.get('reason');target=None
        kind=option['instrument'];underlying=option.get('underlying','')
        if not reason and kind=='OPTIDX' and option['segment'] in ('NSE_FNO','BSE_FNO'):
            target=indices.get((option['exchange'],underlying))
            if target is None:reason='Option index underlying not resolved in Dhan index master'
        elif not reason and kind=='OPTSTK':
            target=cash.get((option['exchange'],option.get('underlying_id','')))
            if target is None:reason='Option stock underlying security ID not resolved in equity master'
        elif not reason and option['segment'] in ('MCX_COMM','NSE_CURRENCY','BSE_CURRENCY'):
            sid=option.get('underlying_id','')
            if not sid.isdigit() or not underlying:reason='Derivative underlying ID or symbol missing'
            else:
                target={**option,'security_id':sid,'symbol':underlying,'instrument':'UNDERLYING',
                        'expiry':None,'strike':None,'side':None,'lot_size':None}
        elif not reason:reason='No verified chain underlying segment mapping'
        if reason:
            unresolved[reason]=unresolved.get(reason,0)+1;continue
        identity=(target['segment'],target['security_id'])
        entry=chosen.setdefault(identity,{**target,'expiry':None,'strike':None,'side':None,
            'chain_underlying':True,'option_sources':[],'underlying_resolution':'Dhan master identity; endpoint availability must be checked'})
        source={'segment':option['segment'],'instrument':kind}
        if source not in entry['option_sources']:entry['option_sources'].append(source)
    return sorted(chosen.values(),key=lambda i:(0 if i['segment']=='IDX_I' else 1,i['segment'],i['symbol'],i['security_id'])),{
        'underlyings':len(chosen),'expired_option_rows_excluded':expired,'unresolved_option_rows':unresolved,
        'coverage':'DISCOVERY_ONLY_NOT_PROVIDER_COVERAGE'}

def quote_probe_items(items):
    # Prefer known quoted indices when present, then sample distinct batch positions.
    priority=sorted(items,key=lambda i:0 if i['symbol'] in ('NIFTY','BANKNIFTY','SENSEX') else 1)
    candidates=priority[:1]+[items[len(items)//2],items[-1]] if items else []
    selected=[];seen=set()
    for item in candidates:
        sid=item['security_id']
        if sid not in seen:seen.add(sid);selected.append(item)
    return selected[:3]

def quote_values(response,segment):
    """Merge isolated diagnostic observations, retaining each original raw response."""
    raw=response.get('raw') or {};data=raw.get('data') or {}
    result=dict(data.get(segment) or {}) if isinstance(data,dict) and isinstance(data.get(segment,{}),dict) else {}
    for probe in response.get('isolated_probes',[]):
        data=(probe.get('raw') or {}).get('data') or {}
        values=data.get(segment,{}) if isinstance(data,dict) else {}
        if isinstance(values,dict):result.update(values)
    return result
