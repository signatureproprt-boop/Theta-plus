"""Whole-master discovery and exact-identity Dhan data access.

The disposable SQLite index contains public instrument metadata only. Raw and
normalized provider observations continue to use the existing durable store.
"""
import asyncio
import anyio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import math

from fastapi import HTTPException
from collection import SEGMENTS, contract_expiry, digest
from market_board import quote_row
from runtime import IST

SEGMENT_NAMES = sorted(set(SEGMENTS.values()))
CATEGORIES={'all':'1=1','indices':"kind='INDEX' AND segment='IDX_I'",
            'cash':"segment IN ('NSE_EQ','BSE_EQ')",'futures':"kind LIKE 'FUT%'",
            'options':"kind LIKE 'OPT%'",'currency':"segment IN ('NSE_CURRENCY','BSE_CURRENCY')",
            'commodities':"segment='MCX_COMM' OR (exchange='NSE' AND raw_segment='M')",
            'unmapped':"segment='UNMAPPED'"}


class InstrumentDirectory:
    def __init__(self, collector):
        self.collector = collector
        self.temporary = tempfile.TemporaryDirectory(prefix='theta-instruments-')
        self.lock = threading.Lock()
        self.snapshot_lock=threading.Lock();self.snapshot_at=0;self.snapshots={};self.snapshot_errors=0

    def index(self):
        cat = self.collector.catalogue()
        if not cat:
            raise HTTPException(409, 'Refresh full instrument catalogue in Collect all data first')
        sha = cat.get('sha256', '')
        if not re.fullmatch('[a-f0-9]{64}', sha):
            raise HTTPException(503, 'Catalogue identity is invalid')
        target = Path(self.temporary.name) / (sha + '-browse-v2.sqlite3')
        if target.is_file():
            return target, cat
        if not self.lock.acquire(timeout=30):
            raise HTTPException(503, 'Instrument search index is preparing; retry shortly')
        pending = target.with_suffix('.building')
        try:
            if target.is_file():
                return target, cat
            pending.unlink(missing_ok=True)
            with closing(sqlite3.connect(pending)) as db:
                db.execute('CREATE TABLE instruments (position INTEGER PRIMARY KEY, key TEXT UNIQUE, segment TEXT, expiry TEXT, search TEXT, document TEXT, kind TEXT, symbol TEXT, exchange TEXT, raw_segment TEXT, rank INTEGER)')
                pages = [p for p in cat['pages'] if p['group'] != 'rolling-underlyings']
                position = 0
                # Bound outstanding page reads; do not load a second full master
                # into RAM while the recorder and historical collector are active.
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for offset in range(0, len(pages), 4):
                        for items in pool.map(lambda p: self.collector.page_items(p['key']), pages[offset:offset+4]):
                            batch = []
                            for item in items:
                                search = ' '.join(str(item.get(k) or '') for k in ('symbol', 'display', 'underlying', 'security_id')).upper()
                                rank=0 if item.get('segment')=='IDX_I' and item.get('security_id') in ('13','25','21') else 1 if item.get('instrument')=='INDEX' else 2 if item.get('instrument')=='EQUITY' else 3
                                batch.append((position, item['key'], item.get('segment') or 'UNMAPPED', contract_expiry(item), search, json.dumps(item),item.get('instrument'),item.get('symbol'),item.get('exchange'),item.get('raw_segment'),rank))
                                position += 1
                            db.executemany('INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?)', batch)
                db.execute('CREATE INDEX segment_expiry ON instruments(segment, expiry)')
                db.execute('CREATE INDEX browse_kind ON instruments(kind,expiry)')
                db.execute('CREATE INDEX browse_order ON instruments(rank,symbol,expiry,position)')
                db.commit()
            os.replace(pending, target)
            for old in target.parent.glob('*.sqlite3'):
                if old != target:
                    old.unlink(missing_ok=True)
            return target, cat
        finally:
            pending.unlink(missing_ok=True)
            self.lock.release()

    @contextmanager
    def database(self):
        # Open under the publication lock. An already open SQLite reader remains
        # valid when a newer immutable master replaces its file on this Linux host.
        for _ in range(2):
            target, cat = self.index()
            with self.lock:
                if not target.is_file():
                    continue
                db = sqlite3.connect('file:'+str(target)+'?mode=ro', uri=True)
                break
        else:
            raise HTTPException(503, 'Catalogue changed during search; retry shortly')
        with closing(db):
            yield db, cat

    def search(self, query='', segment='', page=0, active_only=True,category='all',include_quotes=False):
        if len(query) > 80 or page < 0 or page > 100000 or segment not in ['', 'UNMAPPED', *SEGMENT_NAMES] or category not in CATEGORIES:
            raise HTTPException(422, 'Invalid instrument search or page')
        where, args = ['('+CATEGORIES[category]+')'], []
        if query.strip():
            literal = query.strip().upper().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            where.append("search LIKE ? ESCAPE '\\'")
            args.append('%' + literal + '%')
        if segment:
            where.append('segment=?'); args.append(segment)
        if active_only:
            where.append('(expiry IS NULL OR expiry>=?)'); args.append(datetime.now(IST).date().isoformat())
        clause = ' AND '.join(where)
        with self.database() as (db, cat):
            mapping_total=db.execute("SELECT count(*) FROM instruments WHERE segment='UNMAPPED'").fetchone()[0]
            mapping_current=db.execute("SELECT count(*) FROM instruments WHERE segment='UNMAPPED' AND (expiry IS NULL OR expiry>=?)",[datetime.now(IST).date().isoformat()]).fetchone()[0]
            count = db.execute('SELECT count(*) FROM instruments WHERE '+clause, args).fetchone()[0]
            rows = db.execute('SELECT document FROM instruments WHERE '+clause+' ORDER BY rank,symbol,expiry,position LIMIT 50 OFFSET ?', [*args, page*50]).fetchall()
        items=[json.loads(row[0]) for row in rows]
        if include_quotes:
            observations=self.saved_quotes(items)
            items=[{**i,'saved_quote':observations.get(i['key'])} for i in items]
        return {'catalogue_sha256': cat['sha256'], 'catalogue_at': cat.get('fetched_at'),
                'catalogue_rows': cat['rows'], 'matches': count, 'page': page,
                'next_page': page+1 if (page+1)*50 < count else None,
                'items':items,'category':category,'segments': SEGMENT_NAMES,
                'mapping_review':{'total':mapping_total,'current':mapping_current,'expired':mapping_total-mapping_current,'state':'API_SEGMENT_UNVERIFIED'},
                'review_reasons': cat.get('review_reasons', {}),
                'coverage_note': 'Catalogue discovery is not data coverage. Quotes/history require separate provider responses.'}

    def market_scope(self):
        today=datetime.now(IST).date().isoformat()
        with self.database() as (db,cat):
            rows=db.execute("SELECT exchange,raw_segment,segment,count(*),sum(CASE WHEN expiry IS NULL OR expiry>=? THEN 1 ELSE 0 END) FROM instruments GROUP BY exchange,raw_segment,segment ORDER BY exchange,raw_segment",[today]).fetchall()
        segments=[{'exchange':x,'master_segment':raw,'api_segment':None if segment=='UNMAPPED' else segment,
            'total':total,'current':current,'expired':total-current,'mapping_state':'UNVERIFIED' if segment=='UNMAPPED' else 'DOCUMENTED_SEGMENT'}
            for x,raw,segment,total,current in rows]
        return {'catalogue_sha256':cat['sha256'],'catalogue_at':cat.get('fetched_at'),'catalogue_rows':cat['rows'],
            'mapped_rows':sum(r['total'] for r in segments if r['api_segment']),
            'current_mapped':sum(r['current'] for r in segments if r['api_segment']),
            'unmapped_rows':sum(r['total'] for r in segments if not r['api_segment']),
            'segments':segments}

    def saved_quotes(self,items):
        from data_system import LIVE_STREAMS
        with self.snapshot_lock:
            if time.monotonic()-self.snapshot_at>15:
                def read(stream):
                    try:return self.collector.store.document('system/latest/'+stream)[0] or {}
                    except Exception:return None
                with ThreadPoolExecutor(max_workers=4) as pool:
                    documents=list(pool.map(read,LIVE_STREAMS))
                self.snapshot_errors=sum(d is None for d in documents)
                self.snapshots={(r.get('segment'),str(r.get('security_id'))):r for d in documents if isinstance(d,dict) and isinstance(d.get('rows',{}),dict) for r in d.get('rows',{}).values() if isinstance(r,dict)}
                self.snapshot_at=time.monotonic()
            snapshots=self.snapshots
        result={};clock=datetime.now(timezone.utc)
        for item in items:
            row=snapshots.get((item.get('segment'),str(item.get('security_id'))))
            if not row or item.get('reason') or (item.get('instrument','').startswith(('FUT','OPT')) and row.get('expiry')!=contract_expiry(item)):continue
            if row.get('exchange') and row['exchange']!=item.get('exchange'):continue
            price=row.get('ltp')
            if isinstance(price,bool) or not isinstance(price,(float,int)) or not math.isfinite(price) or price<=0:continue
            try:
                received=datetime.fromisoformat(row['received_at'])
                if received.tzinfo is None or received>clock:continue
            except (KeyError,ValueError,TypeError):continue
            trade=row.get('trade_at')
            try:
                at=datetime.fromisoformat(trade)
                if at.tzinfo is None or at>clock:trade=None
            except (TypeError,ValueError):trade=None
            def numeric(value):return value if not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value) and value>=0 else None
            result[item['key']]={'price':price,'volume':numeric(row.get('volume')),'oi':numeric(row.get('oi')),
                'received_at':row['received_at'],'trade_at':trade,
                'state':'RETAINED_TRADE' if trade else 'TIME_UNVERIFIED','time_basis':'Stored last trade; not certified exchange quote freshness'}
            previous=numeric(row.get('previous_close'))
            if previous:
                result[item['key']].update(previous_close=previous,change=price-previous,change_pct=100*(price-previous)/previous)
        if hasattr(self.collector.store,'live_quotes'):
            try:result.update(self.collector.store.live_quotes(items))
            except Exception:self.snapshot_errors+=1
        return result

    def overview(self,keys=''):
        wanted=keys.split(',') if keys else []
        if len(wanted)>30 or any(not re.fullmatch('[a-zA-Z0-9_-]{1,128}',k) for k in wanted):raise HTTPException(422,'Use at most 30 catalogue keys')
        today=datetime.now(IST).date().isoformat()
        with self.database() as (db,cat):
            mapping_total=db.execute("SELECT count(*) FROM instruments WHERE segment='UNMAPPED'").fetchone()[0]
            counts={name:db.execute('SELECT count(*) FROM instruments WHERE ('+clause+') AND (expiry IS NULL OR expiry>=?)',[today]).fetchone()[0] for name,clause in CATEGORIES.items()}
            if wanted:
                rows=db.execute('SELECT document FROM instruments WHERE key IN ('+','.join('?' for _ in wanted)+')',wanted).fetchall()
                by_key={json.loads(r[0])['key']:json.loads(r[0]) for r in rows};items=[by_key[k] for k in wanted if k in by_key]
            else:
                popular=['ADANIPORTS','BAJFINANCE','MARUTI','M&M','ULTRACEMCO','SUNPHARMA','POWERGRID','TITAN','RELIANCE','INFY','HDFCBANK','ITC']
                rows=db.execute("SELECT document FROM instruments WHERE segment='NSE_EQ' AND kind='EQUITY' AND (symbol IN ("+','.join('?' for _ in popular)+") OR json_extract(document,'$.underlying') IN ("+','.join('?' for _ in popular)+')) ORDER BY position',popular+popular).fetchall()
                by_symbol={}
                for row in rows:
                    item=json.loads(row[0])
                    if not item.get('reason'):
                        name=item['symbol'] if item['symbol'] in popular else item.get('underlying')
                        if name in popular:by_symbol.setdefault(name,item)
                items=[by_symbol[s] for s in popular if s in by_symbol]
                if not items:items=[json.loads(r[0]) for r in db.execute("SELECT document FROM instruments WHERE segment='NSE_EQ' AND kind='EQUITY' ORDER BY rank,symbol LIMIT 12")]
                if not items:items=[json.loads(r[0]) for r in db.execute("SELECT document FROM instruments WHERE segment='IDX_I' ORDER BY rank,symbol LIMIT 12")]
            index_names=['NIFTY','NIFTYNXT50','NIFTYNEXT50','BANKNIFTY','INDIAVIX','FINNIFTY']
            indices=[json.loads(r[0]) for r in db.execute("SELECT document FROM instruments WHERE segment='IDX_I' AND kind='INDEX' AND replace(replace(symbol,' ',''),'_','') IN ("+','.join('?' for _ in index_names)+')',index_names)]
            indices.sort(key=lambda i:index_names.index(re.sub('[ _]','',i['symbol'])))
        quotes=self.saved_quotes(items+indices)
        return {'catalogue_sha256':cat['sha256'],'catalogue_rows':cat['rows'],'catalogue_at':cat.get('fetched_at'),
                'counts':counts,'mapping_review':{'total':mapping_total,'current':counts['unmapped'],'expired':mapping_total-counts['unmapped'],'state':'API_SEGMENT_UNVERIFIED'},'watchlist':[{**i,'saved_quote':quotes.get(i['key'])} for i in items],
                'indices':[{**i,'saved_quote':quotes.get(i['key'])} for i in indices],
                'unresolved':sum(cat.get('review_reasons',{}).values()),'missing_watchlist_keys':[k for k in wanted if k not in {i['key'] for i in items}],
                'snapshot_read_errors':self.snapshot_errors,
                'scope':'Current catalogue counts; watchlist prices refresh every 15 seconds from retained recorder observations, not all-market tick coverage.'}

    def saved_history(self,key,catalogue_sha256,start,end,family='intraday',interval=1):
        if family not in ('daily','intraday') or interval not in (1,5,15,25,60) or not date(1900,1,1)<=start<end<=datetime.now(IST).date() or (end-start).days>31:
            raise HTTPException(422,'Read up to 31 completed days and a supported candle interval')
        item=self.resolve(key,catalogue_sha256);variant={} if family=='daily' else {'interval':str(interval)}
        series=digest({'exchange':item['exchange'],'segment':item['segment'],'sid':item['security_id'],'instrument':item['instrument'],'family':family,'variant':variant})
        days=[(start+timedelta(days=n)).isoformat() for n in range((end-start).days)]
        with ThreadPoolExecutor(max_workers=8) as pool:documents=list(pool.map(lambda d:self.collector.store.document('candles/'+series+'/'+d)[0],days))
        if hasattr(self.collector.store,'native_candle_document'):
            documents=[self.collector.store.native_candle_document(item,series,day,variant,doc) for day,doc in zip(days,documents)]
        candles={};conflicts=set();invalid=0;found=[];clock=datetime.now(timezone.utc)
        for day,doc in zip(days,documents):
            if not doc:continue
            if not isinstance(doc,dict) or not isinstance(doc.get('instrument'),dict) or not isinstance(doc.get('candles'),dict):invalid+=1;continue
            identity=doc.get('instrument',{})
            if doc.get('series')!=series or doc.get('day')!=day or doc.get('variant')!=variant or any(identity.get(k)!=item.get(k) for k in ('exchange','segment','security_id','instrument')):invalid+=1;continue
            if item['instrument'].startswith(('FUT','OPT')) and contract_expiry(identity)!=contract_expiry(item):invalid+=1;continue
            if item['instrument'].startswith('OPT') and any(identity.get(k)!=item.get(k) for k in ('strike','side')):invalid+=1;continue
            found.append(day)
            for row in doc.get('candles',{}).values():
                try:
                    stamp=row['timestamp'];values=[row[k] for k in ('open','high','low','close','volume')]
                    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in [stamp,*values]):raise ValueError()
                    at=datetime.fromtimestamp(stamp,timezone.utc)
                    if int(stamp)!=stamp or int(stamp)%60 or at>clock or at.astimezone(IST).date().isoformat()!=day:raise ValueError()
                    o,h,l,c,v=values
                    if min(o,h,l,c)<=0 or v<0 or h<max(o,l,c) or l>min(o,c):raise ValueError()
                    value={'time':int(stamp),'open':o,'high':h,'low':l,'close':c,'volume':v}
                    if int(stamp) in candles and candles[int(stamp)]!=value:conflicts.add(int(stamp))
                    candles[int(stamp)]=value
                except (KeyError,ValueError,TypeError,OverflowError,OSError):invalid+=1
        rows=[candles[t] for t in sorted(candles) if t not in conflicts];truncated=max(0,len(rows)-50000)
        from history_grid import requested_grid
        sessions=[]
        for day in days:
            selected=[r for r in rows if datetime.fromtimestamp(r['time'],timezone.utc).astimezone(IST).date().isoformat()==day]
            task={'item':item,'family':family,'variant':variant,'from':day,'to':(date.fromisoformat(day)+timedelta(days=1)).isoformat()}
            grid=requested_grid(task,{day:{str(r['time']):{'timestamp':r['time']} for r in selected}})
            sessions.append({'date':day,'received_rows':len(selected),'source_partition_present':day in found,
                'calendar_verified':grid.get('calendar_verified',False),'session_grid_complete':grid.get('session_grid_complete',False) and not (invalid or conflicts),
                'missing_minutes':grid.get('missing_minutes'),'missing_sessions':grid.get('missing_sessions'),
                'state':'SAVED_ROWS_NEED_REVIEW' if invalid or conflicts else grid.get('coverage'),'invalid_or_conflicting_rows_present':bool(invalid or conflicts)})
        return {'instrument':item,'series':series,'family':family,'interval':interval,'from':start.isoformat(),'to_exclusive':end.isoformat(),
                'candles':rows[-50000:],'rows':min(50000,len(rows)),'truncated_rows':truncated,'invalid_excluded':invalid,'conflicting_timestamps':len(conflicts),
                'session_coverage':sessions,'available_days':found,'absent_partitions':[d for d in days if d not in found],
                'state':'RETAINED_ROWS' if rows else 'NO_SAVED_ROWS','coverage_verified':False,
                'coverage_note':'Stored source candles only. Absent partitions are not proof of non-trading days or unavailable provider history; gaps are never filled.'}

    def resolve(self, key, catalogue_sha256, current_quote=False):
        if not isinstance(key, str) or len(key) > 128:
            raise HTTPException(422, 'Invalid instrument key')
        with self.database() as (db, cat):
            if catalogue_sha256 != cat['sha256']:
                raise HTTPException(409, 'Instrument catalogue changed; search and select the instrument again')
            row = db.execute('SELECT document FROM instruments WHERE key=?', (key,)).fetchone()
        if not row:
            raise HTTPException(404, 'Instrument is not in the current catalogue')
        item = json.loads(row[0])
        if current_quote:
            try:age = (datetime.now(timezone.utc)-datetime.fromisoformat(cat['fetched_at'])).total_seconds()
            except (KeyError,TypeError,ValueError):age = float('inf')
            if not -5 <= age <= 86400:
                raise HTTPException(409, 'Catalogue is stale; refresh the full master before fetching current quotes')
        if item.get('reason') or item.get('segment') not in SEGMENT_NAMES or not str(item.get('security_id', '')).isdigit():
            raise HTTPException(409, 'Instrument is unavailable: '+str(item.get('reason') or 'exact segment / identity unresolved'))
        expiry = contract_expiry(item)
        if current_quote and expiry and expiry < datetime.now(IST).date().isoformat():
            raise HTTPException(409, 'Expired contract has no current quote; choose historical data instead')
        return item

    def recorded(self, key, catalogue_sha256):
        from data_system import LIVE_STREAMS, SCOPE
        from sdk_feeds import SEGMENTS as FEED_SEGMENTS
        item = self.resolve(key, catalogue_sha256)
        plan = self.collector.store.document(SCOPE)[0] or {}
        family = next((f for f in LIVE_STREAMS for i in plan.get('scopes', {}).get(f, [])
                       if i.get('segment') == item['segment'] and str(i.get('security_id')) == item['security_id']), None)
        result = {'instrument': item, 'catalogue_sha256': catalogue_sha256, 'recorder': family,
                  'checked_at': datetime.now(timezone.utc).isoformat(), 'observation': None,
                  'state': 'NOT_SUBSCRIBED', 'receipt_age_seconds': None,
                  'coverage_note': 'One retained observation does not certify continuous ticks or exchange quote freshness.'}
        if not family:
            return result
        latest = self.collector.store.document('system/latest/'+family)[0] or {}
        row = latest.get('rows', {}).get(str(FEED_SEGMENTS[item['segment']])+':'+item['security_id'])
        result.update(state='AWAITING_OBSERVATION', subscription_revision=plan.get('feed_revisions', {}).get(family, plan.get('revision')))
        if isinstance(row, dict) and row.get('segment') == item['segment'] and str(row.get('security_id')) == item['security_id']:
            result.update(state='RETAINED_OBSERVATION', observation=row)
            try:result['receipt_age_seconds'] = (datetime.now(timezone.utc)-datetime.fromisoformat(row['received_at'])).total_seconds()
            except (KeyError,TypeError,ValueError):pass
        return result

    async def quote(self, key, catalogue_sha256):
        item = await asyncio.to_thread(self.resolve, key, catalogue_sha256, True)
        with anyio.CancelScope(shield=True):
            owner = await anyio.to_thread.run_sync(self.collector.acquire)
        try:
            raw = await self.collector.call(owner, 'marketfeed/quote', {item['segment']: [int(item['security_id'])]})
            data = raw.get('data') if isinstance(raw, dict) else None
            values = data.get(item['segment'], {}) if isinstance(data, dict) else {}
            value = values.get(str(item['security_id']), {}) if isinstance(values, dict) else {}
            received = datetime.now(timezone.utc)
            identity = {**item, 'label': item.get('display') or item['symbol'], 'group': item['exchange'], 'expiry': contract_expiry(item)}
            result = quote_row(identity, value, received)
            result.update(observation=value, raw=raw, catalogue_sha256=catalogue_sha256)
            if item['instrument'] not in ('FUTIDX', 'FUTSTK', 'FUTCOM', 'FUTCUR'):
                result['note'] = 'Exact selected instrument; last-trade time is not exchange quote time'
            await asyncio.to_thread(self.collector.store.add, 'instrument_quote', result)
            return result
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.collector.release, owner)

    async def history(self, key, catalogue_sha256, start, end, family='intraday', interval=1):
        today = datetime.now(IST).date()
        if family not in ('daily', 'intraday') or interval not in (1, 5, 15, 25, 60):
            raise HTTPException(422, 'Choose daily or intraday data and a supported interval')
        if not date(1900, 1, 1) <= start < end <= today or (end-start).days > (366 if family == 'daily' else 31):
            raise HTTPException(422, 'Use completed dates: up to 31 days intraday or 366 days daily; To is exclusive')
        item = await asyncio.to_thread(self.resolve, key, catalogue_sha256)
        variant = {} if family == 'daily' else {'interval': str(interval)}
        task = {'family': family, 'item': item, 'variant': variant, 'from': start.isoformat(), 'to': end.isoformat()}
        body = {'securityId': str(item['security_id']), 'exchangeSegment': item['segment'],
                'instrument': item['instrument'], 'oi': item['instrument'] not in ('INDEX', 'EQUITY'),
                'fromDate': start.isoformat(), 'toDate': end.isoformat()}
        if family == 'daily':
            body['expiryCode'] = 0
        else:
            body.update(interval=str(interval), fromDate=start.isoformat()+' 00:00:00', toDate=end.isoformat()+' 00:00:00')
        with anyio.CancelScope(shield=True):
            owner = await anyio.to_thread.run_sync(self.collector.acquire)
        try:
            endpoint = 'charts/historical' if family == 'daily' else 'charts/intraday'
            raw = await self.collector.call(owner, endpoint, body)
            received = datetime.now(timezone.utc).isoformat()
            record = {'instrument': item, 'catalogue_sha256': catalogue_sha256, 'request': body,
                      'endpoint': endpoint, 'received_at': received, 'raw': raw}
            # Keep the source even if validation rejects it. Rejections do not
            # become completed normalized histories or advance a job checkpoint.
            await asyncio.to_thread(self.collector.store.add, 'instrument_history_source', record)
            quality = await asyncio.to_thread(self.collector.persist_candles, task, {'raw': raw})
            return {**record, 'quality': quality, 'coverage_note': 'Returned/validated rows are not proof of complete requested-session coverage.'}
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.collector.release, owner)
