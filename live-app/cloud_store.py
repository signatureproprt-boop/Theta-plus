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
