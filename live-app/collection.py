"""Durable Dhan coverage ledger and bounded request-driven collection.

No process-local background task is relied on for Cloud Run persistence.
Every run handles one checkpointed work item under a shared expiring lease.
"""
from __future__ import annotations
import asyncio
import anyio
import base64
import csv
import hashlib
import io
import json
import math
import struct
import time
import uuid
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from functools import lru_cache
import httpx
from pydantic import Field, model_validator
from fastapi import HTTPException
from models import Model
from runtime import IST
from provider import ProviderError
from market_board import MASTER_URL
from collection_contracts import chain_universe,quote_probe_items,quote_values

FAMILIES = ('daily', 'intraday', 'rolling', 'chain', 'quotes', 'live', 'depth20', 'depth200')
SEGMENTS = {('NSE','E'):'NSE_EQ', ('BSE','E'):'BSE_EQ',
            ('NSE','D'):'NSE_FNO', ('BSE','D'):'BSE_FNO',
            ('NSE','C'):'NSE_CURRENCY', ('BSE','C'):'BSE_CURRENCY',
            ('MCX','M'):'MCX_COMM', ('NSE','I'):'IDX_I', ('BSE','I'):'IDX_I'}
TERMINAL = {'completed','partial','empty','unsupported','failed','permission_blocked'}
AUTH_CODES = {'807','808','809','810','DH-901'}
ACCESS_CODES = {'806','DH-902','DH-903'}
SHARD_SIZE = 1000

def now():
    return datetime.now(timezone.utc).isoformat()

def float32_same_price(a,b):
    """Reconcile representation noise only, bounded below half a paisa."""
    if abs(a-b)>0.005:return False
    try:return struct.pack('!f',a)==struct.pack('!f',b)
    except (OverflowError,struct.error):return False

def contract_expiry(item):
    # Master files use placeholder dates even for instruments that never expire.
    # Apply this when reading existing immutable catalogue pages as well as imports.
    return None if item['instrument'] in ('INDEX','EQUITY') else item.get('expiry')

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def install_once(store, key, value):
    if store.put_document(key,value,0):return value
    return store.document(key)[0]

def replace(store, key, fn):
    for _ in range(12):
        value, version = store.document(key)
        changed=fn(value)
        if store.put_document(key,changed,version):return changed
    raise HTTPException(409,'Collection changed concurrently; retry')

class CollectionPlan(Model):
    start: date
    end: date
    continue_rejected_requests: bool = False
    quarantine_invalid_candles: bool = False
    rolling_atm_baseline: bool = False
    families: list[Literal['daily','intraday','rolling','chain','quotes','live','depth20','depth200']] = Field(default_factory=lambda:list(FAMILIES),min_length=1,max_length=8)
    intervals: list[Literal[1,5,15,25,60]] = Field(default_factory=lambda:[1],min_length=1,max_length=5)
    segments: list[str] = Field(default_factory=list,max_length=8)
    symbols: list[str] = Field(default_factory=list,max_length=100)
    @model_validator(mode='after')
    def valid(self):
        if not date(1900,1,1)<=self.start<self.end<=datetime.now(IST).date()+timedelta(days=1):
            raise ValueError('Use a past date range; To is exclusive')
        if any(s not in set(SEGMENTS.values()) for s in self.segments):
            raise ValueError('Unknown API segment')
        self.families=list(dict.fromkeys(self.families))
        self.intervals=list(dict.fromkeys(self.intervals))
        self.symbols=list(dict.fromkeys(s.strip().upper() for s in self.symbols if s.strip()))
        return self

def parse_catalogue(text):
    reader=csv.DictReader(text if hasattr(text,'read') else io.StringIO(text.lstrip('\ufeff')))
    required={'EXCH_ID','SEGMENT','SECURITY_ID','SYMBOL_NAME','INSTRUMENT'}
    if not required.issubset(reader.fieldnames or []):raise ValueError('Instrument master schema changed')
    items=[];seen=set(); counts={};unknown={}
    for line,row in enumerate(reader,2):
        exchange=row.get('EXCH_ID','').strip(); raw_segment=row.get('SEGMENT','').strip()
        segment=SEGMENTS.get((exchange,raw_segment))
        sid=row.get('SECURITY_ID','').strip()
        reason=None
        if not sid.isdigit():reason='Invalid security ID'
        if not segment:reason='No documented API segment mapping: '+exchange+'_'+raw_segment
        expiry=None if row.get('INSTRUMENT','').strip() in ('INDEX','EQUITY') else (row.get('SM_EXPIRY_DATE') or '')[:10] or None
        if expiry:
            try:date.fromisoformat(expiry)
            except ValueError:
                expiry=None
                if row.get('INSTRUMENT') not in ('INDEX','EQUITY'):reason='Invalid contract expiry'
        identity=(exchange,raw_segment,sid)
        if identity in seen:
            reason=reason or 'Duplicate catalogue identity; retained for review'
        seen.add(identity)
        entry={'key':digest([*identity,line]),'security_id':sid,'exchange':exchange,
               'raw_segment':raw_segment,'segment':segment,'instrument':row.get('INSTRUMENT','').strip(),
               'symbol':row.get('SYMBOL_NAME','').strip().upper(),
               'display':row.get('DISPLAY_NAME','').strip(), 'expiry':expiry,
               'underlying_id':(row.get('UNDERLYING_SECURITY_ID') or '').strip(),
               'underlying':(row.get('UNDERLYING_SYMBOL') or '').strip().upper(),
               'strike':row.get('STRIKE_PRICE'),'side':row.get('OPTION_TYPE'),
               'expiry_flag':row.get('EXPIRY_FLAG'),'lot_size':row.get('LOT_SIZE'),
               'tick_size':row.get('TICK_SIZE'),'reason':reason}
        if entry['instrument'] not in ('INDEX','EQUITY') and not expiry:
            entry['reason']=entry['reason'] or 'Contract expiry not resolved'
        items.append(entry)
        group=exchange+'_'+raw_segment
        counts[group]=counts.get(group,0)+1
        if entry['reason']:unknown[entry['reason']]=unknown.get(entry['reason'],0)+1
    if not items:raise ValueError('Instrument master is empty')
    return items,counts,unknown

class Collector:
    def __init__(self,provider,store):
        self.provider=provider
        self.store=store
        self.chain_cache={}

    @lru_cache(maxsize=16)
    def page_items(self,key):
        return self.store.document(key)[0]['items']

    def acquire(self,diagnostic_owner=None):
        owner=uuid.uuid4().hex;ts=time.time()
        priority=self.store.document('control/diagnostic-slot')[0] or {}
        if priority.get('until',0)>ts and priority.get('owner')!=diagnostic_owner:
            raise HTTPException(409,'A historical diagnosis is waiting for the collector')
        def claim(old):
            if old and old.get('until',0)>ts:raise HTTPException(409,'Another collector request is running')
            return {'owner':owner,'until':ts+600,'next_at':(old or {}).get('next_at',0),
                    'day':(old or {}).get('day'),'calls':(old or {}).get('calls',0)}
        replace(self.store,'control/lease',claim)
        return owner

    def release(self,owner):
        def drop(old):
            if old and old.get('owner')==owner:return {**old,'until':0}
            return old
        replace(self.store,'control/lease',drop)

    async def refresh_catalogue(self):
        with anyio.CancelScope(shield=True):
            owner=await anyio.to_thread.run_sync(self.acquire)
        temporary=tempfile.TemporaryDirectory(prefix='theta-catalogue-')
        try:
            source=Path(temporary.name)/'master.csv'
            checksum=hashlib.sha256();size=0
            async with httpx.AsyncClient(timeout=90,follow_redirects=False) as client:
                async with client.stream('GET',MASTER_URL) as response:
                    response.raise_for_status()
                    with source.open('wb') as out:
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>100*1024*1024:raise ValueError('Instrument catalogue exceeds 100 MiB')
                            checksum.update(chunk);out.write(chunk)
            def parse():
                with source.open(encoding='utf-8-sig',newline='') as inp:return parse_catalogue(inp)
            items,counts,unknown=await anyio.to_thread.run_sync(parse)
            sha=checksum.hexdigest()
            def save():
                self.store.put_catalogue_source(sha,source)
                groups={}
                rolling={}
                for item in items:
                    group=(item.get('segment') or 'UNMAPPED')+'-'+item['instrument']
                    groups.setdefault(group,[]).append(item)
                    if item['instrument'] in ('OPTIDX','OPTSTK') and item['segment'] in ('NSE_FNO','BSE_FNO') and item['underlying_id'].isdigit() and not item['reason']:
                        k=(item['segment'],item['underlying_id'],item['instrument'])
                        candidate=rolling.setdefault(k,{**item,'security_id':item['underlying_id'],
                            'symbol':item['underlying'],'expiry':None,'expiry_flags':[]})
                        flag={'W':'WEEK','M':'MONTH'}.get(item['expiry_flag'])
                        if flag and flag not in candidate['expiry_flags']:candidate['expiry_flags'].append(flag)
                groups['rolling-underlyings']=list(rolling.values())
                pages=[]
                tasks=[]
                for group,entries in sorted(groups.items()):
                    for offset in range(0,len(entries),SHARD_SIZE):
                        key='catalogue/'+sha+'/pages/'+str(len(pages)).zfill(5)
                        tasks.append((key,{'items':entries[offset:offset+SHARD_SIZE]}))
                        pages.append({'key':key,'group':group,'count':len(entries[offset:offset+SHARD_SIZE])})
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda pair:install_once(self.store,*pair),tasks))
                manifest={'sha256':sha,'fetched_at':now(),'rows':len(items),'segments':counts,
                          'review_reasons':unknown,'pages':pages,'source':MASTER_URL}
                install_once(self.store,'catalogue/'+sha+'/manifest',manifest)
                replace(self.store,'catalogue/current',lambda _:manifest)
                return manifest
            return await anyio.to_thread.run_sync(save)
        finally:
            with anyio.CancelScope(shield=True):
                temporary.cleanup()
                await anyio.to_thread.run_sync(self.release,owner)

    def catalogue(self):
        return self.store.document('catalogue/current')[0]

    def catalogue_page(self,page=0):
        cat=self.catalogue()
        if not cat:raise HTTPException(409,'Refresh catalogue first')
        if page<0 or page>=len(cat['pages']):return {'items':[],'next_page':None}
        info=cat['pages'][page]
        return {**info,**self.store.document(info['key'])[0],
                'next_page':page+1 if page+1<len(cat['pages']) else None}

    def chain_pages(self,cat):
        cache_key=(cat['sha256'],datetime.now(IST).date().isoformat())
        if cache_key in self.chain_cache:return self.chain_cache[cache_key]
        root='catalogue/'+cat['sha256']+'/chain-v2/'+cache_key[1]
        manifest=self.store.document(root+'/manifest')[0]
        if manifest is None:
            items=[]
            for page in cat['pages']:
                if page['group']=='rolling-underlyings':continue
                items.extend(self.page_items(page['key']))
            entries,discovery=chain_universe(items,datetime.now(IST).date())
            pages=[]
            for offset in range(0,len(entries),SHARD_SIZE):
                key=root+'/pages/'+str(len(pages)).zfill(5)
                install_once(self.store,key,{'items':entries[offset:offset+SHARD_SIZE]})
                pages.append({'key':key,'group':'chain-underlyings','count':len(entries[offset:offset+SHARD_SIZE])})
            manifest=install_once(self.store,root+'/manifest',{'pages':pages,'discovery':discovery})
        self.chain_cache[cache_key]=manifest
        return manifest

    def upgrade_chain_job(self,job):
        # Upgrade only at a safe task boundary. Existing raw data, outcomes and
        # unrelated family cursors stay intact; pending expiry children finish first.
        c=job['cursor'];pages=job['pages']
        if (job.get('chain_planner_version')==2 or job.get('current') or job.get('chain_pending') or
            c['page']>=len(pages) or pages[c['page']]['family']!='chain'):return
        cat=self.store.document('catalogue/'+job['catalogue_sha256']+'/manifest')[0]
        if cat is None:
            cat=self.catalogue()
            if not cat or cat['sha256']!=job['catalogue_sha256']:return
        planned=self.chain_pages(cat);new=[];start=None
        for page in pages:
            if page['family']=='chain':
                if start is None:
                    start=len(new);new.extend({**p,'family':'chain'} for p in planned['pages'])
            else:new.append(page)
        job['chain_planner_upgrade']={'at':now(),'old_cursor':dict(c),'outcomes_retained':job['items_assessed'],
                                      'note':'Underlying discovery rebuilt; prior snapshots remain retained, not coverage certification.'}
        job['pages']=new;job['cursor']={'page':start,'item':0,'variant':0,'window':0}
        job['chain_planner_version']=2;job['chain_discovery']=planned['discovery']

    def create(self,plan,job_id=None,*,catalogue=None):
        if job_id:
            if len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):raise ValueError('Invalid internal job identity')
            existing=self.store.document('jobs/'+job_id)[0]
            if existing:return self.summary(existing)
        cat=catalogue if catalogue is not None else self.catalogue()
        if not cat:raise HTTPException(409,'Refresh instrument catalogue first')
        config=plan.model_dump(mode='json')
        # Preserve every entry; filters select groups, never silently rewrite master.
        pages=[]
        for family in config['families']:
            if family=='chain':
                pages.extend({**p,'family':family} for p in self.chain_pages(cat)['pages'])
                continue
            for p in sorted(cat['pages'],key=lambda p:(0 if p['group']=='IDX_I-INDEX' else 1 if p['group'].startswith('NSE_EQ-') else 2,p['key'])):
                rolling=p['group']=='rolling-underlyings'
                if (family=='rolling')!=rolling:continue
                if config['segments'] and not rolling and not any(p['group'].startswith(segment+'-') for segment in config['segments']):continue
                pages.append({**p,'family':family})
        job={'id':job_id or uuid.uuid4().hex,'created_at':now(),'updated_at':now(),'state':'paused',
             'catalogue_sha256':cat['sha256'],'catalogue_rows':cat['rows'],'config':config,
             'pages':pages,'cursor':{'page':0,'item':0,'variant':0,'window':0},
             'counts':{s:0 for s in TERMINAL},'rows_saved':0,'items_assessed':0,
             'provider_calls':0,'message':'Ready. Resume to collect. Runs while this page is open or a runner calls run.',
             'retries':0,'chain_planner_version':2,'rolling_contract_version':2,'rolling_identity_version':2}
        if 'chain' in config['families']:job['chain_discovery']=self.chain_pages(cat)['discovery']
        self.store.put_document('jobs/'+job['id'],job,0)
        return self.summary(job)

    @staticmethod
    def summary(job):
        result={k:v for k,v in job.items() if k not in ('pages','current','chain_parent','chain_pending')}
        if job.get('current'):
            t=job['current']
            result['current_item']={'family':t['family'],'symbol':t['item']['symbol'],'segment':t['item']['segment'],'from':t['from'],'to':t['to']}
        return result

    def get_job(self,jid):
        if len(jid)!=32 or any(c not in '0123456789abcdef' for c in jid):raise HTTPException(404,'Job not found')
        job=self.store.document('jobs/'+jid)[0]
        if not job:raise HTTPException(404,'Job not found')
        return job

    def jobs(self,after=''):
        names=self.store.document_names('jobs/',after,21)
        jobs=[];issues=[]
        for name in names[:20]:
            job=self.store.document(name)[0]
            # Listing and reading are separate operations. A retained null
            # document or a disappeared object must not crash both workspaces.
            if not isinstance(job,dict) or not job:
                issues.append({'document':name,'state':'JOB_RECORD_UNAVAILABLE'})
                continue
            jobs.append(self.summary(job))
        return {'jobs':jobs,'read_issues':issues,
                'next_cursor':names[19] if len(names)>20 else None}

    def record_rejection(self,job,task,reason,code=None,http_status=None):
        """Explicit failure outcome, never successful coverage or unsupported inference."""
        key=digest({k:v for k,v in task.items() if k not in ('skip','horizon_gap')})
        if task['family'] not in ('daily','intraday','rolling'):key=digest([job['id'],key])
        install_once(self.store,'results/'+job['id']+'/'+key,{
            'task':task,'checked_at':now(),'result':{'status':'failed','rows':0,'reason':reason,
            'code':code,'http_status':http_status,'coverage':'NOT_COLLECTED','review_required':True}})
        job['counts']['failed']+=1;job['items_assessed']+=1
        if task['family']=='chain_snapshot':
            job['chain_pending'].pop(0)
            if job['chain_pending']:job['current']={**job['chain_parent'],'family':'chain_snapshot','expiry':job['chain_pending'][0]}
            else:job.pop('current',None);job.pop('chain_parent',None)
        else:self.advance(job,task);job.pop('current',None)
        job['retries']=0;job.pop('retry_at',None);job.pop('request_error',None)

    def control(self,jid,action):
        self.get_job(jid)
        def change(job):
            if action=='pause':
                job['state']='paused'
                if job.get('system_role'):job['system_user_paused']=True
            elif action=='resume':
                if job['state']=='completed':return job
                job['state']='running';job['message']='Running in bounded requests'
                job.pop('system_user_paused',None)
            elif action=='continue-rejected':
                if job['state']!='request_review' or not job.get('current'):
                    raise HTTPException(409,'No rejected work item awaiting review')
                err=job.get('request_error') or {}
                if err.get('code') not in ('DH-905','DH-907') and err.get('http_status') not in (400,422):
                    raise HTTPException(409,'This error requires manual review')
                job['config']['continue_rejected_requests']=True
                self.record_rejection(job,job['current'],job['message'],err.get('code'),err.get('http_status'))
                job['state']='paused';job['message']='Rejected request recorded as failed, not collected. Resume continues other data; further invalid/no-data requests are recorded as failed. Token, permission and rate-limit errors still stop or back off.'
            elif action=='stop-on-rejected':
                if job['state']=='running':raise HTTPException(409,'Pause the job before changing rejection policy')
                job['config']['continue_rejected_requests']=False
            elif action=='skip-blocked':
                if job['state']!='permission_blocked' or not job.get('current'):
                    raise HTTPException(409,'No permission-blocked work item')
                task=job['current'];key=digest(task)
                install_once(self.store,'results/'+jid+'/'+key,{'task':task,'result':{'status':'permission_blocked','rows':0,'reason':job['message']},'checked_at':now()})
                job['counts']['permission_blocked']+=1;job['items_assessed']+=1
                if task['family']=='chain_snapshot':
                    job['chain_pending'].pop(0)
                    if job['chain_pending']:job['current']={**job['chain_parent'],'family':'chain_snapshot','expiry':job['chain_pending'][0]}
                    else:job.pop('current',None)
                else:self.advance(job,task);job.pop('current',None)
                job['state']='paused';job['message']='Blocked item recorded. Resume to continue other items.'
            else:raise HTTPException(422,'Unknown action')
            job['updated_at']=now()
            return job
        return self.summary(replace(self.store,'jobs/'+jid,change))

    def select_item(self,job):
        """Advance local cursor only; caller checkpoints it under the lease."""
        c=job['cursor'];config=job['config']
        while c['page']<len(job['pages']):
            page=job['pages'][c['page']]
            entries=self.page_items(page['key'])
            if c['item']>=len(entries):
                c.update(page=c['page']+1,item=0,variant=0,window=0)
                continue
            item=entries[c['item']]
            if ((config['segments'] and item['segment'] not in config['segments']) or
                (config['symbols'] and item['symbol'] not in config['symbols'] and item['underlying'] not in config['symbols'])):
                c.update(item=c['item']+1,variant=0,window=0)
                continue
            return page['family'],item
        return None,None

    def variants(self,family,item,job):
        cfg=job['config']
        if family=='rolling':
            if cfg.get('rolling_atm_baseline'):
                return [{'expiryFlag':'MONTH','expiryCode':1,'strike':'ATM','drvOptionType':side,'interval':str(interval)}
                        for side in ('CALL','PUT') for interval in cfg['intervals']]
            result=[]
            flags=(['MONTH','WEEK'] if job.get('rolling_identity_version')==2 else ['WEEK','MONTH']) if item['instrument']=='OPTIDX' else (item.get('expiry_flags') or ['MONTH'])
            for flag in flags:
                # Rolling options use 1/2/3, unlike the daily-history enum.
                # Retain legacy variant positions until the run guard stops
                # that job; never reinterpret an already saved cursor.
                codes=(1,2,3) if job.get('rolling_contract_version')==2 else (0,1,2)
                for code in codes:
                    reach=10 if item['instrument']=='OPTIDX' and code==codes[0] else 3
                    for strike in ['ATM']+[f'ATM{n:+}' for n in range(-reach,reach+1) if n]:
                        for side in ('CALL','PUT'):
                            for interval in cfg['intervals']:
                                result.append({'expiryFlag':flag,'expiryCode':code,'strike':strike,'drvOptionType':side,'interval':str(interval)})
            return result
        return [{'interval':str(i)} for i in cfg['intervals']] if family=='intraday' else [{}]

    def rolling_index_item(self,job,item):
        if item['instrument']!='OPTIDX':return item
        cat=self.store.document('catalogue/'+job['catalogue_sha256']+'/manifest')[0]
        if not cat:
            cat=self.catalogue()
            if not cat or cat['sha256']!=job['catalogue_sha256']:
                return {**item,'reason':'Pinned index master unavailable; rolling underlying identity unresolved'}
        matches=[]
        for page in cat['pages']:
            if page['group']!='IDX_I-INDEX':continue
            matches.extend(row for row in self.page_items(page['key'])
                           if row['exchange']==item['exchange'] and row['symbol']==item['symbol']
                           and not row.get('reason'))
        if len(matches)!=1:
            return {**item,'reason':'Rolling index underlying does not resolve uniquely in pinned Dhan index master'}
        return {**item,'security_id':matches[0]['security_id'],
                'source_underlying_id':item['security_id'],'rolling_identity_source':'PINNED_DHAN_INDEX_MASTER'}

    def task(self,job,family,item):
        if family=='rolling' and job.get('rolling_identity_version')==2:
            item=self.rolling_index_item(job,item)
        cursor=job['cursor'];variants=self.variants(family,item,job)
        if cursor['variant']>=len(variants):
            cursor.update(item=cursor['item']+1,variant=0,window=0)
            return None
        variant=variants[cursor['variant']]
        start=date.fromisoformat(job['config']['start']);end=date.fromisoformat(job['config']['end'])
        skip=item.get('reason')
        if family in ('intraday','rolling'):
            horizon=datetime.now(IST).date()-timedelta(days=5*365+2)
            if start<horizon:
                # Explicit coverage gap, then advance to the supported horizon.
                if not cursor.get('horizon_ack'):
                    return {'family':family,'item':item,'variant':variant,'from':start.isoformat(),
                            'to':min(end,horizon).isoformat(),'skip':'Outside documented five-year history horizon','horizon_gap':True}
                start=max(start,horizon)
        expiry=contract_expiry(item)
        if family=='intraday' and expiry and expiry<datetime.now(IST).date().isoformat():
            return {'family':family,'item':item,'variant':variant,'from':start.isoformat(),'to':end.isoformat(),
                    'skip':skip or 'Active intraday endpoint does not establish expired-contract history; use documented rolling options separately'}
        if family in ('daily','intraday') and expiry:
            end=min(end,date.fromisoformat(expiry)+timedelta(days=1))
        window_days=30 if family=='rolling' else 90 if family=='intraday' else max(1,(end-start).days)
        if family in ('daily','intraday','rolling'):
            start=start+timedelta(days=cursor['window']*window_days)
            end=min(end,start+timedelta(days=window_days))
            if start>=end:
                cursor.update(variant=cursor['variant']+1,window=0)
                cursor.pop('horizon_ack',None)
                return None
        if family=='chain':
            if not item.get('chain_underlying') and item['instrument'] not in ('INDEX','EQUITY'):skip=skip or 'Chain requested via underlying entries only'
            if item['symbol'] in ('INDIA VIX','INDIAVIX'):skip=skip or 'No options for this underlying'
        if family in ('depth20','depth200') and item['segment'] not in ('NSE_EQ','NSE_FNO'):
            skip=skip or 'Full depth documented for NSE equity and derivatives only'
        if family in ('quotes','live','depth20','depth200','chain') and expiry and expiry<datetime.now(IST).date().isoformat():
            skip=skip or 'Expired contract is not a current feed instrument'
        if family=='intraday' and expiry and expiry<datetime.now(IST).date().isoformat():
            skip=skip or 'Active intraday endpoint does not establish expired-contract history; use documented rolling options separately'
        task={'family':family,'item':item,'variant':variant,'from':start.isoformat(),
                'to':end.isoformat(),'skip':skip}
        if skip and family in ('quotes','live','depth20','depth200'):
            entries=self.page_items(job['pages'][cursor['page']]['key']);batch=[]
            for candidate in entries[cursor['item']:]:
                candidate_expiry=contract_expiry(candidate)
                candidate_reason=candidate.get('reason')
                if not candidate_reason and family in ('depth20','depth200') and candidate['segment'] not in ('NSE_EQ','NSE_FNO'):
                    candidate_reason='Full depth documented for NSE equity and derivatives only'
                if not candidate_reason and candidate_expiry and candidate_expiry<datetime.now(IST).date().isoformat():
                    candidate_reason='Expired contract is not a current feed instrument'
                if candidate_reason!=skip or candidate['segment']!=item['segment']:break
                if ((config:=job['config'])['segments'] and candidate['segment'] not in config['segments']) or (config['symbols'] and candidate['symbol'] not in config['symbols'] and candidate['underlying'] not in config['symbols']):break
                batch.append(candidate)
                if len(batch)>=1000:break
            if batch:task.update(batch=batch,span=len(batch))
        if not skip and family in ('quotes','live','depth20'):
            maximum={'quotes':1000,'live':100,'depth20':50}[family]
            entries=self.page_items(job['pages'][cursor['page']]['key'])
            batch=[];span=0
            for candidate in entries[cursor['item']:]:
                expiry=contract_expiry(candidate)
                if candidate.get('reason') or (expiry and expiry<datetime.now(IST).date().isoformat()):break
                if candidate['segment']!=item['segment']:break
                if family=='depth20' and candidate['segment'] not in ('NSE_EQ','NSE_FNO'):break
                span+=1
                if ((not job['config']['segments'] or candidate['segment'] in job['config']['segments']) and
                    (not job['config']['symbols'] or candidate['symbol'] in job['config']['symbols'] or candidate['underlying'] in job['config']['symbols'])):
                    batch.append(candidate)
                if len(batch)>=maximum:break
            task['batch']=batch;task['span']=span
        return task

    @staticmethod
    def advance(job,task):
        cursor=job['cursor']
        if task.get('horizon_gap'):
            cursor['horizon_ack']=True
        elif task.get('batch'):
            cursor.update(item=cursor['item']+task['span'],variant=0,window=0)
        elif task['family'] in ('daily','intraday','rolling') and not task.get('skip'):
            cursor['window']+=1
        else:
            cursor.update(variant=cursor['variant']+1,window=0)
            cursor.pop('horizon_ack',None)

    async def call(self,owner,path,body):
        # Shared ledger serializes quota across jobs and Cloud Run instances.
        lease,_=await asyncio.to_thread(self.store.document,'control/lease')
        if lease.get('owner')!=owner:raise HTTPException(409,'Collector lease lost')
        wait=lease.get('next_at',0)-time.time()
        if wait>0:await asyncio.sleep(min(wait,3.2))
        today=datetime.now(IST).date().isoformat()
        def reserve(old):
            if old.get('owner')!=owner:raise HTTPException(409,'Collector lease lost')
            calls=old.get('calls',0) if old.get('day')==today else 0
            if calls>=100000:raise ProviderError('Daily API budget reached',code='BUDGET')
            return {**old,'day':today,'calls':calls+1,'next_at':time.time()+3.2}
        await asyncio.to_thread(replace,self.store,'control/lease',reserve)
        return await self.provider.post(path,body)

    async def acquire_data(self,owner,task):
        family=task['family'];item=task['item'];sid=item['security_id'];segment=item['segment']
        inst=item['instrument'];variant=task['variant']
        if family in ('daily','intraday','rolling'):
            if family=='rolling':
                body={'exchangeSegment':segment,'securityId':int(sid),'instrument':inst,**variant,
                      'requiredData':['open','high','low','close','iv','volume','strike','oi','spot'],
                      'fromDate':task['from'],'toDate':task['to']}
                endpoint='charts/rollingoption'
            else:
                body={'exchangeSegment':segment,'securityId':sid,'instrument':inst,
                      'oi':inst not in ('EQUITY','INDEX'),'fromDate':task['from'],'toDate':task['to'],**variant}
                endpoint='charts/historical' if family=='daily' else 'charts/intraday'
                if family=='daily':body['expiryCode']=0
                else:body.update(fromDate=task['from']+' 00:00:00',toDate=task['to']+' 00:00:00')
            raw=await self.call(owner,endpoint,body)
            return {'endpoint':endpoint,'request':body,'raw':raw,'received_at':now()}
        if family=='chain':
            args={'UnderlyingScrip':int(sid),'UnderlyingSeg':segment}
            expiries=(await self.call(owner,'optionchain/expirylist',args)).get('data')
            if not isinstance(expiries,list):raise ProviderError('Expiry list response invalid')
            # Expiries themselves are persisted before the chain child cursor runs.
            return {'endpoint':'optionchain/expirylist','request':args,'raw':{'data':expiries},'received_at':now()}
        if family=='quotes':
            body={segment:[int(i['security_id']) for i in task.get('batch',[item])]}
            response={'endpoint':'marketfeed/quote','request':body,
                    'raw':await self.call(owner,'marketfeed/quote',body),'received_at':now()}
            expected=task.get('batch',[item]);values=quote_values(response,segment)
            if len(expected)>1 and not any(isinstance(values.get(i['security_id']),dict) and values[i['security_id']] for i in expected):
                response['isolated_probes']=[]
                for candidate in quote_probe_items(expected):
                    probe_body={segment:[int(candidate['security_id'])]}
                    probe={'request':probe_body}
                    try:probe['raw']=await self.call(owner,'marketfeed/quote',probe_body)
                    except ProviderError as exc:
                        if exc.code in AUTH_CODES|ACCESS_CODES|{'805','DH-904','BUDGET'} or exc.status in (401,403,429):raise
                        probe.update(error=str(exc),code=exc.code,http_status=exc.status)
                    probe['received_at']=now();response['isolated_probes'].append(probe)
                response['diagnosis']='Empty bulk response; bounded isolated probes retained. Cause is not inferred from market closure.'
            return response
        return await self.capture(task)

    async def capture(self,task):
        """One instrument, ten-second binary capture. Never persists auth URL."""
        from urllib.parse import urlencode
        import websockets
        from sdk_feeds import make_codec,decode_packet,feed_info
        family=task['family'];item=task['item']
        instruments=task.get('batch',[item])
        codec=await asyncio.to_thread(make_codec,self.provider.credentials,family,instruments)
        params=urlencode({'version':'2','token':self.provider.credentials.token(),
                          'clientId':self.provider.credentials.client_id,'authType':'2'})
        host=('wss://api-feed.dhan.co?' if family=='live' else
              'wss://depth-api-feed.dhan.co/twentydepth?' if family=='depth20' else
              'wss://full-depth-api.dhan.co/?')
        # Record the official SDK-generated subscription, without auth URLs.
        subscriptions=[]
        class SubscriptionSocket:
            async def send(self,packet):subscriptions.append(json.loads(packet))
        codec.ws=SubscriptionSocket()
        await codec.subscribe_instruments()
        body={'subscriptions':subscriptions}
        frames=[];started=now();total_bytes=0;truncated=False
        try:
            async with websockets.connect(host+params,open_timeout=10,close_timeout=2,max_size=2**20) as ws:
                for subscription in subscriptions:await ws.send(json.dumps(subscription))
                deadline=time.monotonic()+10
                while time.monotonic()<deadline and len(frames)<1000:
                    try:packet=await asyncio.wait_for(ws.recv(),max(.01,deadline-time.monotonic()))
                    except asyncio.TimeoutError:break
                    if isinstance(packet,bytes):
                        total_bytes+=len(packet)
                        if total_bytes>8*1024*1024:
                            truncated=True;break
                        offset=0 if family=='live' else 2
                        if len(packet)>=(10 if family=='live' else 14) and packet[offset]==50:
                            code=int.from_bytes(packet[8:10] if family=='live' else packet[12:14],'little')
                            raise ProviderError('Dhan feed disconnected',code=str(code))
                        observations=[];position=0
                        while position<len(packet):
                            part=packet[position:]
                            minimum=8 if family=='live' else 12
                            if len(part)<minimum:raise ProviderError('Truncated live feed header')
                            size=int.from_bytes(part[1:3] if family=='live' else part[0:2],'little')
                            if size<minimum or size>len(part):raise ProviderError('Invalid live feed packet length')
                            code=part[offset];security_id=str(int.from_bytes(part[4:8],'little'))
                            if code in ((1,2,4,5,6,8) if family=='live' else (41,51)):
                                observation={'security_id':security_id,'packet_code':code}
                                try:
                                    decoded=decode_packet(codec,part[:size])
                                    if decoded is not None:observation['decoded']=decoded
                                except Exception:observation['decode_status']='SDK_DECODE_FAILED_RAW_RETAINED'
                                observations.append(observation)
                            position+=size
                        frames.append({'received_at':now(),'binary_base64':base64.b64encode(packet).decode(),'observations':observations})
                    else:raise ProviderError('Unexpected text response from binary market feed')
        except ProviderError:raise
        except Exception:
            # Exceptions can contain the credential-bearing URL; never propagate.
            raise ProviderError('Live feed unavailable; check token, entitlement and market session') from None
        return {'endpoint':family,'request':body,'raw':{'frames':frames},**feed_info(family),
                'started_at':started,'received_at':now(),'format':'Dhan binary frames, base64 encoded',
                'capture_truncated':truncated or len(frames)>=1000,
                'freshness':'Receipt only; exchange timestamps not decoded. This is a bounded sample, not continuous recording.'}

    def persist_candles(self,task,response,quarantine_invalid=False):
        raw=response['raw'];family=task['family'];item=task['item']
        row=(raw.get('data') or {}).get('ce' if task['variant']['drvOptionType']=='CALL' else 'pe') if family=='rolling' else raw
        if row is None:return {'rows':0,'duplicate_timestamps':0,'quality':'EMPTY'}
        required=['timestamp','open','high','low','close','volume']
        if family=='rolling':required+=['strike','oi','iv','spot']
        elif item['instrument'] not in ('INDEX','EQUITY'):required+=['open_interest']
        if not isinstance(row,dict) or any(not isinstance(row.get(k),list) for k in required):
            raise ProviderError('Required historical arrays missing')
        n=len(row['timestamp'])
        if any(len(row[k])!=n for k in required):raise ProviderError('Historical arrays differ in length')
        by_day={};duplicates=0;outside_range=0;precision_rows=0;observed=set();quarantined=[]
        for i,stamp in enumerate(row['timestamp']):
            if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp):
                raise ProviderError('Invalid historical timestamp')
            at=datetime.fromtimestamp(stamp,timezone.utc).astimezone(IST)
            if int(stamp)!=stamp or int(stamp)%60:
                raise ProviderError('Historical timestamp is not on a minute boundary')
            if not task['from']<=at.date().isoformat()<task['to']:
                if family!='rolling':raise ProviderError('Historical timestamp outside requested range')
                # Rolling responses can include their end-date boundary. Raw is
                # retained; only the requested interval enters normalized storage.
                outside_range+=1;continue
            candle={k:row[k][i] for k in required}
            if any(isinstance(candle[k],bool) or not isinstance(candle[k],(int,float)) or not math.isfinite(candle[k]) for k in required):
                raise ProviderError('Non-numeric historical value')
            # Keep raw provider arrays unchanged. Bounds may mix decimal and
            # float32 encodings of the same price; larger inconsistencies fail.
            adjusted=False
            upper=max(candle['open'],candle['close'],candle['low'])
            lower=min(candle['open'],candle['close'])
            if candle['high']<upper and float32_same_price(candle['high'],upper):
                candle['high']=upper;adjusted=True
            if candle['low']>lower and float32_same_price(candle['low'],lower):
                candle['low']=lower;adjusted=True
            if (min(candle[k] for k in ('open','high','low','close'))<0 or candle['volume']<0 or
                candle['high']<max(candle['open'],candle['close'],candle['low']) or
                candle['low']>min(candle['open'],candle['close'])):
                if not quarantine_invalid:raise ProviderError('Invalid OHLC or volume')
                quarantined.append({'row_index':i,'timestamp':stamp,'reason':'Invalid OHLC or volume'})
                continue
            if any(candle.get(k,0)<0 for k in ('open_interest','oi','iv','spot','strike')):raise ProviderError('Negative derivative data')
            precision_rows+=int(adjusted)
            key=str(int(stamp))+(':'+str(float(candle['strike'])) if family=='rolling' else '')
            if key in observed:duplicates+=1
            observed.add(key)
            by_day.setdefault(at.date().isoformat(),{})[key]=candle
        series=digest({'exchange':item['exchange'],'segment':item['segment'],'sid':item['security_id'],'instrument':item['instrument'],
                       'family':family,'variant':task['variant']})
        def merge(pair):
            day,values=pair
            def change(old):
                return {'series':series,'day':day,'instrument':item,'variant':task['variant'],
                        'candles':{**(old or {}).get('candles',{}),**values},'updated_at':now(),
                        'calendar_check':'Pending exchange calendar; no gaps filled'}
            replace(self.store,'candles/'+series+'/'+day,change)
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(merge,by_day.items()))
        quarantine_key=None
        if quarantined:
            quarantine_key='quarantine/'+digest([series,task['from'],task['to'],response.get('received_at'),quarantined])
            install_once(self.store,quarantine_key,{'series':series,'from':task['from'],'to':task['to'],
                         'received_at':response.get('received_at'),'excluded_rows':quarantined,
                         'policy':'Raw arrays retained; invalid rows excluded, never filled or corrected.'})
        from history_grid import requested_grid
        grid=requested_grid(task,by_day)
        return {'rows':len(observed),'duplicate_timestamps':duplicates,'outside_requested_dates':outside_range,'quality':'PARTIAL' if quarantined and observed else 'INVALID_ONLY' if quarantined else 'ROWS_PRESENT' if observed else 'EMPTY',
                'quarantined_rows':len(quarantined),'quarantine_key':quarantine_key,
                'precision_reconciled_rows':precision_rows,'precision_policy':'same_float32_within_0.005',
                'session_validation':'Requested regular-session grid checked' if grid.get('calendar_verified') else 'Pending correct exchange calendar',
                'days':len(by_day),'days_saved':sorted(by_day),'series':series,
                **grid,'coverage':'PARTIAL_INVALID_ROWS' if quarantined else grid['coverage'],
                'requested_from':task['from'],'requested_to_exclusive':task['to'],
                'first_returned_at':datetime.fromtimestamp(min(int(k.split(':')[0]) for k in observed),timezone.utc).isoformat() if observed else None,
                'last_returned_at':datetime.fromtimestamp(max(int(k.split(':')[0]) for k in observed),timezone.utc).isoformat() if observed else None}

    def persist_result(self,task,response,quarantine_invalid=False):
        family=task['family']
        if family in ('daily','intraday','rolling'):return self.persist_candles(task,response,quarantine_invalid)
        if family=='chain':
            expiries=response['raw'].get('data',[])
            if any(not isinstance(e,str) for e in expiries):raise ProviderError('Invalid expiry date')
            try:
                for e in expiries:date.fromisoformat(e)
            except ValueError:raise ProviderError('Invalid expiry date') from None
            return {'rows':len(expiries),'expiries':expiries,'quality':'ROWS_PRESENT' if expiries else 'EMPTY'}
        if family=='chain_snapshot':
            oc=(response['raw'].get('data') or {}).get('oc')
            if not isinstance(oc,dict):raise ProviderError('Chain strike map missing')
            return {'rows':len(oc),'quality':'ROWS_PRESENT' if oc else 'EMPTY','freshness':'Server receipt only'}
        if family=='quotes':
            quotes=quote_values(response,task['item']['segment'])
            expected=[i['security_id'] for i in task.get('batch',[task['item']])]
            valid=[sid for sid in expected if isinstance(quotes,dict) and isinstance(quotes.get(sid),dict) and quotes[sid]]
            return {'rows':len(valid),'quality':'PARTIAL' if valid and len(valid)<len(expected) else 'ROWS_PRESENT' if valid else 'EMPTY','expected':len(expected),
                    'missing_security_ids':[sid for sid in expected if sid not in valid],
                    'coverage':'PARTIAL' if len(valid)<len(expected) else 'SNAPSHOT_RETURNED_NOT_CONTINUOUS',
                    'isolated_probes':len(response.get('isolated_probes',[])),
                    'review_required':len(valid)<len(expected),'reason':response.get('diagnosis') if not valid else None,
                    'freshness':'Inspect provider last_trade_time; receipt is not a live quote timestamp'}
        expected={i['security_id'] for i in task.get('batch',[task['item']])}
        def decoded(o):return o['security_id'] in expected and isinstance(o.get('decoded'),dict) and bool(o['decoded'])
        observed={o['security_id'] for f in response['raw']['frames'] for o in f.get('observations',[]) if decoded(o)}
        matched=sum(any(decoded(o) for o in f.get('observations',[])) for f in response['raw']['frames'])
        decode_failed=sum(o['security_id'] in expected and not decoded(o) for f in response['raw']['frames'] for o in f.get('observations',[]))
        return {'rows':matched,'quality':'ROWS_PRESENT' if matched else 'EMPTY','frames_received':len(response['raw']['frames']),
                'decode_failed_observations':decode_failed,
                'missing_security_ids':sorted(expected-observed),'continuous':False,'exchange_time_verified':False}

    async def run(self,jid):
        with anyio.CancelScope(shield=True):
            owner=await anyio.to_thread.run_sync(self.acquire)
        try:
            job=await asyncio.to_thread(self.get_job,jid)
            if job['state']!='running':return self.summary(job)
            if job.get('retry_at',0)>time.time():return self.summary(job)
            await asyncio.to_thread(self.upgrade_chain_job,job)
            task=job.get('current')
            for _ in range(200):
                if task:break
                family,item=await asyncio.to_thread(self.select_item,job)
                if not item:
                    if job['items_assessed']==0:
                        job['state']='no_work';job['message']='No work items were assessed and no data was collected. Review symbol, segment and date filters; this is not successful coverage.'
                    else:
                        job['state']='completed';job['message']='Every selected item has an outcome. Review empty, failed and unsupported counts.'
                    break
                task=self.task(job,family,item)
            if not task:
                await asyncio.to_thread(self.save_job,job)
                return self.summary(job)
            if task['family'] in ('live','depth20','depth200'):
                system=(await asyncio.to_thread(self.store.document,'control/data-system'))[0] or {}
                if system.get('enabled'):
                    job.update(state='paused',message='Continuous recorder owns feed connections. Sample checkpoint retained.',current=task)
                    await asyncio.to_thread(self.save_job,job)
                    return self.summary(job)
            if task['family']=='rolling' and (job.get('rolling_contract_version')!=2 or job.get('rolling_identity_version')!=2) and not job.get('retry_of'):
                job['current']=task
                job['state']='request_review'
                job['message']='Legacy rolling expiry or underlying mapping stopped before a provider call. Create a new Expired options plan using codes 1/2/3 and pinned Dhan index identities. Original cursor, results and gaps are retained.'
                job['updated_at']=now()
                await asyncio.to_thread(self.save_job,job)
                return self.summary(job)
            job['current']=task
            await asyncio.to_thread(self.save_job,job)
            key=digest({k:v for k,v in task.items() if k not in ('skip','horizon_gap')})
            # Snapshots are scoped to the job; historical tasks reuse raw and normalized data.
            if task['family'] not in ('daily','intraday','rolling'):key=digest([jid,key])
            raw_key='responses/'+key
            if job.get('force_fetch'):raw_key='responses/'+digest([key,jid])
            try:
                if task.get('skip'):
                    result={'status':'unsupported','reason':task['skip'],'rows':0,'coverage':'NOT_COLLECTED',
                            'instruments_assessed':len(task.get('batch',[task['item']]))}
                else:
                    response,_=await asyncio.to_thread(self.store.document,raw_key)
                    if response is not None and task['family'] in ('daily','intraday','rolling') and not job.get('force_fetch'):
                        from history_grid import cache_is_closed
                        if not cache_is_closed(task,response):
                            # Preserve the pre-close response. A fresh immutable
                            # archive belongs to this job, never to the old key.
                            raw_key='responses/'+digest([key,jid,'closed-range-v1'])
                            response,_=await asyncio.to_thread(self.store.document,raw_key)
                    if response is None:
                        job['provider_calls']+=1
                        if task['family']=='chain_snapshot':
                            body={'UnderlyingScrip':int(task['item']['security_id']),'UnderlyingSeg':task['item']['segment'],'Expiry':task['expiry']}
                            response={'endpoint':'optionchain','request':body,'received_at':now(),
                                      'raw':await self.call(owner,'optionchain',body)}
                        else:response=await self.acquire_data(owner,task)
                        await asyncio.to_thread(install_once,self.store,raw_key,response)
                    recovery_policy=(await asyncio.to_thread(self.store.document,'control/gap-recovery'))[0] or {}
                    quarantine=job['config'].get('quarantine_invalid_candles',False) or recovery_policy.get('enabled',False)
                    result=await asyncio.to_thread(self.persist_result,task,response,quarantine)
                    from research_archive import publish
                    await asyncio.to_thread(publish,self.store,task,response,result)
                    result['status']=('failed' if not result['rows'] and (result.get('quarantined_rows') or result.get('decode_failed_observations')) else
                                      'partial' if result['rows'] and (result.get('missing_security_ids') or result.get('quarantined_rows') or result.get('decode_failed_observations') or result.get('missing_sessions') or result.get('missing_minutes')) else
                                      'completed' if result['rows'] else 'empty')
                    result['raw_key']=raw_key
                entry={'task':task,'result':result,'checked_at':now()}
                await asyncio.to_thread(install_once,self.store,'results/'+jid+'/'+key,entry)
                job['counts'][result['status']]=job['counts'].get(result['status'],0)+1;job['rows_saved']+=result['rows'];job['items_assessed']+=1
                job['retries']=0;job.pop('retry_at',None);job.pop('request_error',None)
                if task['family']=='chain' and result.get('expiries'):
                    job['chain_pending']=[e for e in result['expiries'] if e>=datetime.now(IST).date().isoformat()]
                    job['chain_parent']=task
                if task['family']=='chain_snapshot':
                    job['chain_pending'].pop(0)
                else:self.advance(job,task)
                job.pop('current',None)
                if job.get('chain_pending'):
                    job['current']={**job['chain_parent'],'family':'chain_snapshot','expiry':job['chain_pending'][0]}
                else:job.pop('chain_parent',None)
                job['message']=('Valid rows saved; invalid candles quarantined. Historical coverage remains incomplete.'
                                if result.get('quarantined_rows') else 'Saved work item. Empty and unsupported items remain visible.')
            except ProviderError as exc:
                code=exc.code
                job['message']=str(exc)
                if code in AUTH_CODES or exc.status==401:
                    job['state']='token_required'
                elif code=='BUDGET':
                    job['state']='paused';job['provider_calls']=max(0,job['provider_calls']-1)
                elif code in ACCESS_CODES or exc.status==403:
                    job['state']='permission_blocked'
                elif exc.status==429 or code in ('DH-904','805'):
                    job['retries']+=1;job['retry_at']=time.time()+min(300,5*2**min(job['retries'],6))
                    job['message']='Rate limited; backing off at the same checkpoint. No data was marked complete.'
                elif code in ('DH-905','DH-907') or exc.status in (400,422):
                    # A rejected request needs review, not repeated identical calls or cursor advancement.
                    if job['config'].get('continue_rejected_requests'):
                        self.record_rejection(job,task,str(exc),code,exc.status)
                        job['message']='Rejected request recorded as failed / NOT_COLLECTED. Continuing other data. Review failed results; this is not complete historical coverage.'
                    else:
                        job['state']='request_review'
                        job['request_error']={'code':code,'http_status':exc.status,'checked_at':now()}
                        job['retries']=0;job.pop('retry_at',None)
                        job['message']=str(exc)+'. Collection stopped at this item. Review dates or explicitly record rejection and continue other data. No data was marked complete.'
                else:
                    job['retries']+=1
                    if job['retries']<3:
                        job['retry_at']=time.time()+min(300,5*2**job['retries'])
                    else:
                        result={'status':'failed','reason':str(exc),'code':code,'rows':0,'raw_key':raw_key}
                        await asyncio.to_thread(install_once,self.store,'results/'+jid+'/'+key,{'task':task,'result':result,'checked_at':now()})
                        job['counts']['failed']+=1;job['items_assessed']+=1;job['retries']=0
                        if task['family']=='chain_snapshot':
                            job['chain_pending'].pop(0)
                            if job['chain_pending']:
                                job['current']={**job['chain_parent'],'family':'chain_snapshot','expiry':job['chain_pending'][0]}
                            else:job.pop('current',None)
                        else:
                            self.advance(job,task);job.pop('current',None)
            job['updated_at']=now()
            await asyncio.to_thread(self.save_job,job)
            return self.summary(job)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.release,owner)

    def save_job(self,job):
        def save(old):
            # A pause from another request wins over the in-flight result.
            if old and old['state']=='paused' and job['state']=='running':
                job['state']='paused'
                if old.get('system_user_paused'):job['system_user_paused']=True
            if old and old.get('system_role'):job['system_role']=old['system_role']
            return job
        return replace(self.store,'jobs/'+job['id'],save)

    def results(self,jid,after=''):
        self.get_job(jid)
        prefix='results/'+jid+'/'
        if after and not after.startswith(prefix):raise HTTPException(422,'Invalid cursor')
        names=self.store.document_names(prefix,after,51)
        return {'items':[{'key':k,**self.store.document(k)[0]} for k in names[:50]],
                'next_cursor':names[49] if len(names)>50 else None}

    def retry(self,jid,key,quarantine_invalid=False,child_id=None):
        self.get_job(jid)
        if not key.startswith('results/'+jid+'/'):raise HTTPException(422,'Invalid result key')
        entry=self.store.document(key)[0]
        if not entry or entry['result']['status'] not in ('failed','partial','empty','permission_blocked'):
            raise HTTPException(422,'Choose a failed, partial or empty result')
        if entry['task']['family']=='rolling' and entry['task']['variant'].get('expiryCode') not in (1,2,3):
            raise HTTPException(422,'Invalid legacy rolling expiry code. Create a new Expired options plan; the original failure remains retained.')
        parent=self.get_job(jid)
        if entry['task']['family']=='rolling' and entry['task']['item']['instrument']=='OPTIDX' and parent.get('rolling_identity_version')!=2:
            raise HTTPException(422,'Legacy rolling index identity requires a new Expired options plan; original results retained.')
        if child_id:
            existing=self.store.document('jobs/'+child_id)[0]
            if existing:return self.summary(existing)
        child={**parent,'id':child_id or uuid.uuid4().hex,'pages':[],'cursor':{'page':0,'item':0,'variant':0,'window':0},
            'created_at':now(),'updated_at':now(),'state':'paused','current':entry['task'],
            'counts':{s:0 for s in TERMINAL},'items_assessed':0,'rows_saved':0,'retries':0,
            'provider_calls':0,'message':'Retry ready. Resume to retry this result.','retry_of':key,'force_fetch':True}
        child.pop('retry_at',None)
        child['config']={**parent['config']}
        if quarantine_invalid:child['config']['quarantine_invalid_candles']=True
        if entry['task']['family'] in ('quotes','live','depth20','depth200') and entry['result'].get('missing_security_ids'):
            missing=set(entry['result']['missing_security_ids'])
            task={**entry['task']};batch=[i for i in task.get('batch',[task['item']]) if i['security_id'] in missing]
            if not batch:raise HTTPException(422,'Missing IDs do not match the retained subscription')
            task.update(item=batch[0],batch=batch,span=len(batch));child['current']=task
        if entry['task']['family']=='chain_snapshot':
            child['chain_parent']=entry['task'];child['chain_pending']=[entry['task']['expiry']]
        self.store.put_document('jobs/'+child['id'],child,0)
        return self.summary(child)
