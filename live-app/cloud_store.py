"""Immutable private GCS records; never stores the database on ephemeral disk."""
import json
import secrets
import time
from datetime import datetime, timezone
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from google.cloud import storage
from google.api_core.exceptions import NotFound, PreconditionFailed

MAX_ID = 9007199254740991  # JavaScript's exact integer boundary

class CloudStore:
    def __init__(self, bucket_name, client=None):
        self.bucket = (client or storage.Client()).bucket(bucket_name)

    @staticmethod
    def document_key(name):
        if not name or any(p in ('','.','..') for p in name.split('/')) or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./' for c in name):
            raise ValueError('Invalid document name')
        return 'collection/'+name+'.json'

    def document(self, name):
        # Mutable manifests must bypass the immutable-record cache. Pin reads to
        # the generation obtained by reload, otherwise CAS could pair old/new bytes.
        key=self.document_key(name)
        for _ in range(3):
            blob=self.bucket.blob(key)
            try:
                blob.reload(timeout=30)
                generation=int(blob.generation)
                return json.loads(blob.download_as_text(if_generation_match=generation,timeout=30)),generation
            except NotFound:return None,0
            except PreconditionFailed:continue
        raise RuntimeError('Concurrent document update; retry request')

    def put_document(self, name, payload, version):
        try:
            self.bucket.blob(self.document_key(name)).upload_from_string(json.dumps(payload,allow_nan=False),
                content_type='application/json',if_generation_match=version,timeout=30)
            return True
        except PreconditionFailed:return False

    def document_names(self, prefix, after='', limit=100):
        root='collection/'
        args={'prefix':root+prefix,'max_results':limit+1 if after else limit}
        if after:args['start_offset']=root+after+'.json'
        return [b.name[len(root):-5] for b in self.bucket.list_blobs(**args)
                if b.name.endswith('.json') and b.name[len(root):-5]>after][:limit]

    def diagnostic_names(self,namespace,after='',limit=25):
        root={'documents':'collection/','records':'records/','journal':'journal/'}.get(namespace)
        if root is None:raise ValueError('Unknown diagnostic namespace')
        args={'prefix':root,'page_size':limit}
        if after:args['start_offset']=root+after+'.json'
        names=[]
        # CSV sources do not end the JSON traversal or consume its page limit.
        for blob in self.bucket.list_blobs(**args):
            if blob.name.endswith('.json') and blob.name[len(root):-5]>after:
                names.append(blob.name[len(root):-5])
                if len(names)==limit:break
        return names

    def diagnostic_read(self,namespace,key):
        if namespace=='documents':return self.document(key)[0]
        root={'records':'records/','journal':'journal/'}.get(namespace)
        if root is None:raise ValueError('Unknown diagnostic namespace')
        result=self.read(root+key+'.json')
        return {**result,'kind':key.split('/')[0]} if namespace=='records' else result

    def put_catalogue_source(self, sha, source):
        if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('Invalid checksum')
        try:
            self.bucket.blob(f'collection/catalogue/{sha}/source.csv').upload_from_filename(
                str(source),content_type='text/csv',if_generation_match=0,timeout=90)
        except PreconditionFailed:pass

    @staticmethod
    def key(kind, record_id):
        if not kind.replace('_', '').isalnum():
            raise ValueError('Invalid record kind')
        return f'records/{kind}/{MAX_ID-int(record_id):016d}.json'

    def write_once(self, key, payload):
        try:
            self.bucket.blob(key).upload_from_string(
                json.dumps(payload, allow_nan=False), content_type='application/json',
                if_generation_match=0, timeout=30)
            return True
        except PreconditionFailed:
            return False

    @lru_cache(maxsize=32)
    def read_text(self, key):
        # Cache immutable records as text; callers receive independent objects.
        return self.bucket.blob(key).download_as_text(timeout=30)

    def read(self, key):
        return json.loads(self.read_text(key))

    def add(self, kind, payload):
        for _ in range(10):
            record_id = int(time.time()*1000)*1000 + secrets.randbelow(1000)
            row = {'id': record_id, 'created_at': datetime.now(timezone.utc).isoformat(), 'data': payload}
            if self.write_once(self.key(kind, record_id), row):
                return record_id
        raise RuntimeError('Could not allocate a unique record ID')

    def get(self, kind, id):
        try:
            return self.read(self.key(kind, id))
        except NotFound:
            return None

    def rows(self, kind, limit=100):
        prefix = self.key(kind, 0).rsplit('/', 1)[0] + '/'
        names = [b.name for b in self.bucket.list_blobs(prefix=prefix, max_results=limit)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(self.read, names))

    def journal_add(self, plan_id, payload):
        # Fixed key gives atomic idempotency across instances and restarts.
        import uuid
        canonical = str(uuid.UUID(str(plan_id)))
        return self.write_once(f'journal/{canonical}.json', {
            **payload, 'plan_id': plan_id, 'created_at': datetime.now(timezone.utc).isoformat()})

    def journal_rows(self):
        # Journal is small in this single-user app; sort before applying the cap.
        names = [b.name for b in self.bucket.list_blobs(prefix='journal/')]
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(self.read, names))
        return sorted(rows, key=lambda r:r['created_at'], reverse=True)[:1000]
