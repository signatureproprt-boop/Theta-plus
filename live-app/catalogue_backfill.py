"""Checkpoint historical work for identities introduced by later Dhan masters."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from collection import CollectionPlan,SHARD_SIZE,digest,install_once,replace
from runtime import IST

KEY='system/backfill-catalogue'

def identity(item,rolling=False):
    fields=('exchange','raw_segment','security_id','segment','instrument','symbol','reason')
    if not rolling:fields+=('expiry','underlying_id','strike','side')
    return tuple(item.get(k) for k in fields)+(tuple(sorted(item.get('expiry_flags') or [])) if rolling else (),)

def additions(collector,previous,current,rolling):
    """Compare immutable masters, one group at a time; row numbers aren't IDs."""
    groups=defaultdict(list);old=defaultdict(list)
    for page in previous['pages']:
        if (page['group']=='rolling-underlyings')==rolling:old[page['group']].append(page)
    for page in current['pages']:
        if (page['group']=='rolling-underlyings')==rolling:groups[page['group']].append(page)
    pages=[];count=0
    root='catalogue/'+current['sha256']+'/additions-v1/'+previous['sha256']
    with ThreadPoolExecutor(max_workers=8) as pool:
        for group,entries in sorted(groups.items()):
            known={identity(i,rolling) for items in pool.map(collector.page_items,[p['key'] for p in old[group]]) for i in items}
            buffer=[]
            def save():
                key=root+'/'+('rolling' if rolling else 'instruments')+'/'+str(len(pages)).zfill(5)
                install_once(collector.store,key,{'items':list(buffer)})
                pages.append({'key':key,'group':group,'count':len(buffer)})
            for items in pool.map(collector.page_items,[p['key'] for p in entries]):
                for item in items:
                    mark=identity(item,rolling)
                    if mark in known:continue
                    known.add(mark);buffer.append(item);count+=1
                    if len(buffer)==SHARD_SIZE:save();buffer=[]
            if buffer:save()
    return pages,count

def schedule(collector,clock):
    store=collector.store;roots=(store.document('system/backfill')[0] or {}).get('jobs',{})
    current=collector.catalogue()
    if not roots or not current:return
    state=store.document(KEY)[0] or {'families':{}}
    families=dict(state.get('families',{}));changed=False;comparisons={}
    for family in ('daily','intraday','rolling'):
        if family not in roots:continue
        slot=families.get(family,{})
        if slot.get('catalogue_sha256')==current['sha256']:continue
        parent=collector.get_job(roots[family])
        if family not in parent['config']['families'] or parent['config'].get('symbols') or parent['config'].get('segments'):
            raise ValueError('Backfill root does not cover the full family universe')
        previous_sha=slot.get('catalogue_sha256') or parent['catalogue_sha256']
        if previous_sha==current['sha256']:pages=[];count=0
        else:
            previous=store.document('catalogue/'+previous_sha+'/manifest')[0]
            if not previous or previous.get('sha256')!=previous_sha:
                raise ValueError('Previous pinned catalogue unavailable; historical additions not certified')
            pair=(previous_sha,family=='rolling')
            if pair not in comparisons:comparisons[pair]=additions(collector,previous,current,family=='rolling')
            pages,count=comparisons[pair]
        jid=None
        if pages:
            jid=digest(['catalogue-history-v1',family,previous_sha,current['sha256']])[:32]
            plan=CollectionPlan(start=parent['config']['start'],end=clock.astimezone(IST).date(),
                families=[family],intervals=parent['config']['intervals'],
                continue_rejected_requests=True,quarantine_invalid_candles=True)
            collector.create(plan,job_id=jid,catalogue=current) # Pin this exact comparison snapshot.
            def prepare(job):
                if job.get('catalogue_additions'):return job
                if job['state']!='paused' or job['items_assessed'] or job.get('current'):
                    raise ValueError('Additions job already advanced before its scope was installed')
                return {**job,'pages':[{**p,'family':family} for p in pages],
                    'system_role':'catalogue_backfill_'+family,
                    'catalogue_additions':{'previous_sha256':previous_sha,'current_sha256':current['sha256'],'identities':count},
                    'message':'Historical additions queued; original backfill checkpoints and failures retained.'}
            job=replace(store,'jobs/'+jid,prepare)
            if job['state']=='paused' and not job.get('system_user_paused') and not parent.get('system_user_paused'):
                collector.control(jid,'resume')
        families[family]={'catalogue_sha256':current['sha256'],'previous_sha256':previous_sha,
            'job_id':jid,'added_identities':count,'checked_at':clock.isoformat()};changed=True
        # Commit each family after its job is durable. Deterministic IDs make
        # a crash between job creation and this checkpoint safe to replay.
        replace(store,KEY,lambda old:{**(old or {}),'families':families,
            'updated_at':clock.isoformat(),'coverage_verified':False,
            'scope':'New or changed master identities only; scheduling is not collected historical coverage.'})
    return changed
