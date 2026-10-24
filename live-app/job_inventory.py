"""Generation-checked GCS job snapshots for discovery, never request control.

Listing generations are compared on every scan. Collector.run and all CAS
updates continue through fresh CloudStore.document reads.
"""
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from google.api_core.exceptions import NotFound,PreconditionFailed


class JobInventory:
    def __init__(self,store,max_bytes=32*1024*1024,max_entries=2048):
        self.store=store;self.max_bytes=max_bytes;self.max_entries=max_entries
        self.cache=OrderedDict();self.bytes=0;self.lock=threading.Lock();self.stats={}

    def remember(self,name,generation,text):
        size=len(text.encode('utf-8'))
        with self.lock:
            old=self.cache.pop(name,None)
            if old:self.bytes-=old[2]
            if size>self.max_bytes:return
            self.cache[name]=(generation,text,size);self.bytes+=size
            while self.bytes>self.max_bytes or len(self.cache)>self.max_entries:
                _,old=self.cache.popitem(last=False);self.bytes-=old[2]

    def read(self,blob):
        name=blob.name;generation=int(blob.generation)
        with self.lock:
            hit=self.cache.get(name)
            if hit and hit[0]==generation:
                self.cache.move_to_end(name)
                return json.loads(hit[1]),True
        try:
            text=blob.download_as_text(if_generation_match=generation,timeout=30)
            data=json.loads(text)
        except (NotFound,PreconditionFailed):
            # The listed generation changed/deleted before download. A fresh
            # versioned read resolves that race; never reuse the older snapshot.
            key=name[len('collection/'):-len('.json')]
            data,generation=self.store.document(key)
            if data is None:
                with self.lock:
                    old=self.cache.pop(name,None)
                    if old:self.bytes-=old[2]
                return None,False
            text=json.dumps(data,allow_nan=False)
        self.remember(name,generation,text)
        return data,False

    def documents(self):
        started=time.monotonic();listed=hits=downloads=0;seen=set()
        blobs=self.store.bucket.list_blobs(prefix='collection/jobs/',page_size=100)
        batch=[]
        with ThreadPoolExecutor(max_workers=8) as pool:
            def read_batch(entries):
                nonlocal listed,hits,downloads
                for data,hit in pool.map(self.read,entries):
                    listed+=1;hits+=int(hit);downloads+=int(not hit)
                    if data is not None:yield data
            for blob in blobs:
                if not blob.name.endswith('.json'):continue
                seen.add(blob.name);batch.append(blob)
                if len(batch)==100:
                    yield from read_batch(batch);batch=[]
            if batch:yield from read_batch(batch)
        with self.lock:
            for name in list(self.cache):
                if name not in seen:
                    self.bytes-=self.cache.pop(name)[2]
            self.stats={'listed_jobs':listed,'cache_hits':hits,'source_reads':downloads,
                'cache_entries':len(self.cache),'cache_bytes':self.bytes,
                'duration_seconds':round(time.monotonic()-started,3),
                'basis':'Job discovery only; source generations checked each scan. Request control always reads fresh state.'}

    def status(self):
        with self.lock:return dict(self.stats)
