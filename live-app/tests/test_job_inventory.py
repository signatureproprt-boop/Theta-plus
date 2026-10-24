import json
from copy import deepcopy
from types import SimpleNamespace
from google.api_core.exceptions import PreconditionFailed
from job_inventory import JobInventory


class Blob:
    def __init__(self,bucket,name,generation):self.bucket=bucket;self.name=name;self.generation=generation
    def download_as_text(self,if_generation_match,timeout):
        self.bucket.reads+=1
        generation,data=self.bucket.rows[self.name]
        if generation!=if_generation_match:raise PreconditionFailed('Changed after listing')
        return json.dumps(data)


class Bucket:
    def __init__(self):self.rows={};self.reads=0;self.after_list=None
    def list_blobs(self,**kwargs):
        blobs=[Blob(self,name,generation) for name,(generation,_) in sorted(self.rows.items())]
        if self.after_list:self.after_list();self.after_list=None
        return blobs


def setup():
    bucket=Bucket();store=SimpleNamespace(bucket=bucket)
    def document(key):
        generation,data=bucket.rows['collection/'+key+'.json'];return deepcopy(data),generation
    store.document=document
    inventory=JobInventory(store);bucket.rows={'collection/jobs/a.json':(1,{'id':'a','state':'running'}),
        'collection/jobs/b.json':(2,{'id':'b','state':'completed'})}
    return bucket,inventory


def test_unchanged_generations_avoid_downloads_and_changed_pause_is_seen():
    bucket,inventory=setup()
    assert len(list(inventory.documents()))==2 and bucket.reads==2
    cached=list(inventory.documents());assert bucket.reads==2
    cached[0]['state']='caller mutation'
    bucket.rows['collection/jobs/a.json']=(3,{'id':'a','state':'paused','system_user_paused':True})
    rows=list(inventory.documents())
    assert bucket.reads==3 and rows[0]['state']=='paused' and rows[0]['system_user_paused']
    assert inventory.status()['cache_hits']==1 and inventory.status()['source_reads']==1
    assert list(inventory.documents())[0]['state']=='paused'


def test_race_after_listing_uses_fresh_version_without_caching_under_old_generation():
    bucket,inventory=setup()
    bucket.after_list=lambda:bucket.rows.update({'collection/jobs/a.json':(7,{'id':'a','state':'paused'})})
    rows=list(inventory.documents())
    assert rows[0]['state']=='paused' and inventory.cache['collection/jobs/a.json'][0]==7
    reads=bucket.reads;assert list(inventory.documents())[0]['state']=='paused' and bucket.reads==reads


def test_cache_is_bounded_and_deleted_jobs_do_not_survive_listing():
    bucket,inventory=setup();inventory.max_entries=1
    list(inventory.documents());assert len(inventory.cache)==1
    bucket.rows={};assert list(inventory.documents())==[]
    assert len(inventory.cache)==0 and inventory.bytes==0
    inventory.remember('large',1,'x'*(inventory.max_bytes+1));assert inventory.bytes==0
