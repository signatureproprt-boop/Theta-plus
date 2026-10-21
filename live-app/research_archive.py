"""Bounded read index connecting collected index evidence to research.

Raw archives/checkpoints are retained. Daily, rolling and non-index prices never
enter the 1-minute chart. Receipt timestamps do not become exchange quote times.
"""
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor


def index_symbol(item):
    if item.get('segment')!='IDX_I' or item.get('instrument')!='INDEX' or item.get('exchange')!='NSE':return None
    return {('13','NIFTY'):'nifty',('25','BANKNIFTY'):'banknifty'}.get((str(item.get('security_id')),item.get('symbol')))


def publish(store,task,response,result):
    from collection import replace
    symbol=index_symbol(task['item'])
    if not symbol or not result.get('rows'):return
    received=response.get('received_at')
    if not received:return
    if task['family']=='intraday' and str(task['variant'].get('interval'))=='1':
        # Small index of retained day partitions. No second copy of candle rows.
        def update(old):
            days=dict((old or {}).get('days',{}))
            for day in result['days_saved']:
                entry={'key':'candles/'+result['series']+'/'+day,'received_at':received}
                if day not in days or days[day]['received_at']<=received:days[day]=entry
            return {'symbol':symbol,'days':{d:days[d] for d in sorted(days)[-90:]},'coverage':'RETAINED_ROWS_NOT_CERTIFIED'}
        replace(store,'research/collected/history/'+symbol,update)
    elif task['family']=='chain_snapshot':
        expiry=task['expiry']
        record={'metadata':{'index':symbol,'expiry':expiry,'fetched_at':received,'exchange_at':None,
                            'source':'collector optionchain'},'raw':response['raw']}
        # Index embeds up to 10 current expiries; readers need one document.
        def update(old):
            snapshots=dict((old or {}).get('snapshots',{}))
            previous=snapshots.get(expiry)
            if not previous or previous['metadata']['fetched_at']<=received:snapshots[expiry]=record
            from runtime import IST
            today=datetime.now(IST).date().isoformat()
            return {'symbol':symbol,'snapshots':{d:snapshots[d] for d in [d for d in sorted(snapshots) if d>=today][:10]},
                    'coverage':'SNAPSHOTS_NOT_CONTINUOUS'}
        replace(store,'research/collected/chain/'+symbol,update)


def history_days(store,symbol,clock):
    from research import stamp
    from runtime import IST
    from collection import digest
    manifest=store.document('research/collected/history/'+symbol)[0] or {}
    selected=[(day,item) for day,item in sorted(manifest.get('days',{}).items())
              if day<=clock.date().isoformat() and stamp(item.get('received_at')) and stamp(item['received_at'])<=clock][-21:]
    if not selected and symbol in ('nifty','banknifty'):
        # Existing collector partitions predate this index. Resolve their exact
        # deterministic series, never scan unrelated instruments or rolling data.
        series=digest({'exchange':'NSE','segment':'IDX_I','sid':'13' if symbol=='nifty' else '25',
                       'instrument':'INDEX','family':'intraday','variant':{'interval':'1'}})
        today=clock.astimezone(IST).date()
        selected=[((today-timedelta(days=i)).isoformat(),{'key':'candles/'+series+'/'+(today-timedelta(days=i)).isoformat()}) for i in range(20,-1,-1)]
    result=[]
    with ThreadPoolExecutor(max_workers=8) as pool:
        documents=list(pool.map(lambda pair:store.document(pair[1]['key'])[0],selected))
    for (day,item),document in zip(selected,documents):
        if not document or not stamp(document.get('updated_at')) or stamp(document['updated_at'])>clock:continue
        if index_symbol(document.get('instrument',{}))!=symbol or str(document.get('variant',{}).get('interval'))!='1':continue
        result.append((item['key'],item.get('received_at') or document['updated_at'],list(document.get('candles',{}).values())))
    return result


def chain_record(store,symbol,clock,legacy=None):
    from research import stamp
    from runtime import IST
    manifest=store.document('research/collected/chain/'+symbol)[0] or {}
    records=list(manifest.get('snapshots',{}).values())+([legacy] if legacy else [])
    candidates=[r for r in records if r.get('metadata',{}).get('index')==symbol
                and r['metadata'].get('expiry','')>=clock.astimezone(IST).date().isoformat()
                and stamp(r['metadata'].get('fetched_at')) and stamp(r['metadata']['fetched_at'])<=clock]
    if not candidates:return legacy
    expiry=min(r['metadata']['expiry'] for r in candidates)
    return max((r for r in candidates if r['metadata']['expiry']==expiry),key=lambda r:stamp(r['metadata']['fetched_at']))
