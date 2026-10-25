"""Bounded-memory, generation-pinned, resumable owner archive download.

Uses the existing gcloud login. Access tokens stay in memory, never in files/logs.
Source objects are read-only. No broker requests or instrument mapping occur here.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
from http.client import HTTPException, HTTPSConnection, IncompleteRead
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

BUCKET = 'theta-pluse-theta-plus-data'
MAX_OBJECT_BYTES = 256 * 1024**2
DISK_RESERVE = 256 * 1024**2
PAGE_SIZE = 200
WORKERS = 12
AUTH_RETRY_SECONDS = 30
AUTH_ATTEMPTS = 21  # Wait up to ten minutes for owner Cloud Shell authorization.


class MediaResponse:
    """Release a fully consumed response; discard connections on partial reads."""
    def __init__(self, response, connection):
        self.response, self.connection = response, connection
        self.status = response.status

    def read(self, *args):
        try:
            return self.response.read(*args)
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        if not self.response.isclosed():
            self.connection.close()
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class MediaConnections:
    """One verified-TLS connection per worker, pinned to the GCS host."""
    def __init__(self):
        self.local = threading.local()

    def open(self, url, headers):
        target = urlsplit(url)
        if (target.scheme != 'https' or target.netloc != 'storage.googleapis.com' or
                target.fragment or not target.path.startswith('/storage/v1/b/' + BUCKET + '/o/')):
            raise RuntimeError('UNEXPECTED_STORAGE_MEDIA_DESTINATION')
        connection = getattr(self.local, 'connection', None)
        if connection is None:
            # HTTPSConnection uses a default verifying SSL context; never disables TLS.
            connection = self.local.connection = HTTPSConnection('storage.googleapis.com', timeout=45)
        path = target.path + ('?' + target.query if target.query else '')
        try:
            connection.request('GET', path, headers=headers)
            return MediaResponse(connection.getresponse(), connection)
        except BaseException:
            connection.close()
            raise


@contextmanager
def workspace():
    root = Path.home() / '.local/state/theta-unified-merge'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise RuntimeError('UNSAFE_MERGE_WORKSPACE')
    root.chmod(0o700)
    with (root / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('MERGE_ALREADY_RUNNING; inspect theta-merge.log') from None
        yield root


def safe_path(root, name):
    parts = PurePosixPath(name).parts
    if (not name or name.startswith('/') or '\\' in name or '\x00' in name or
            any(p in ('', '.', '..') for p in name.split('/')) or
            name == 'migration-manifest.json' or len(name) > 1024):
        raise RuntimeError('UNSAFE_SOURCE_OBJECT_PATH')
    destination = root.joinpath(*parts)
    for parent in (destination, *destination.parents):
        if parent.is_symlink():
            raise RuntimeError('SYMLINK_IN_ARCHIVE')
        if parent == root:
            break
    return destination


def file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class Storage:
    def __init__(self, cloud):
        self.cloud = cloud
        self.token = None
        self.token_at = 0
        self.lock = threading.Lock()
        self.media_connections = MediaConnections()

    def access_token(self, force=False):
        with self.lock:
            if force or self.token is None or time.monotonic() - self.token_at > 2700:
                for attempt in range(AUTH_ATTEMPTS):
                    try:
                        token = self.cloud('auth', 'print-access-token', capture=True).strip()
                        if not token:
                            raise ValueError('EMPTY_ACCESS_TOKEN')
                    except (subprocess.CalledProcessError, OSError, ValueError):
                        # Do not print subprocess output: it may contain credentials.
                        # Keep the scan/checkpoints alive while the owner reauthorizes
                        # in another terminal. The lock prevents parallel refresh storms.
                        if attempt == AUTH_ATTEMPTS - 1:
                            raise RuntimeError('GOOGLE_AUTH_WAIT_EXHAUSTED; verified files retained') from None
                        print('GOOGLE_AUTH_WAIT; authorize Google Cloud in another terminal; retry in',
                              AUTH_RETRY_SECONDS, 'seconds;',
                              time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), flush=True)
                        time.sleep(AUTH_RETRY_SECONDS)
                        continue
                    self.token = token
                    self.token_at = time.monotonic()
                    if attempt:
                        print('GOOGLE_AUTH_RESUMED; continuing current archive scan', flush=True)
                    break
            return self.token

    def open(self, url, raw=False):
        for attempt in range(4):
            try:
                headers = {
                    'Authorization': 'Bearer ' + self.access_token(),
                    # Prevent transparent GCS gzip decompression: preserve source bytes.
                    'Accept-Encoding': 'gzip' if raw else 'identity',
                }
                if raw:
                    response = self.media_connections.open(url, headers)
                    if response.status != 200:
                        code = response.status
                        response.close()
                        raise HTTPError(url, code, 'Storage media response', {}, None)
                    return response
                return urlopen(Request(url, headers=headers), timeout=45)
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code == 401 and attempt < 3:
                    self.access_token(force=True)
                    continue
                if code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError('STORAGE_HTTP_' + str(code)) from None
            except (URLError, OSError, HTTPException):
                if attempt == 3:
                    raise RuntimeError('STORAGE_NETWORK_RETRIES_EXHAUSTED') from None
            time.sleep(2**attempt)

    def pages(self):
        page_token = None
        previous = None
        while True:
            query = {'maxResults': PAGE_SIZE,
                'fields': 'nextPageToken,items(name,generation,size,md5Hash,crc32c)'}
            if page_token:
                query['pageToken'] = page_token
            with self.open('https://storage.googleapis.com/storage/v1/b/' + BUCKET + '/o?' + urlencode(query)) as response:
                page = json.load(response)
            items = page.get('items', [])
            if not isinstance(items, list) or len(items) > PAGE_SIZE:
                raise RuntimeError('UNEXPECTED_STORAGE_PAGE')
            for item in items:
                if previous is not None and item['name'] <= previous:
                    raise RuntimeError('STORAGE_LIST_ORDER_CHANGED')
                previous = item['name']
            yield items
            next_token = page.get('nextPageToken')
            if not next_token:
                break
            if next_token == page_token:
                raise RuntimeError('STORAGE_PAGE_TOKEN_REPEATED')
            page_token = next_token

    def media(self, item):
        return self.open('https://storage.googleapis.com/storage/v1/b/' + BUCKET +
            '/o/' + quote(item['name'], safe='') + '?' + urlencode({
                'alt': 'media', 'generation': item['generation']}), raw=True)


def inventory_digest(storage):
    digest = hashlib.sha256()
    count = 0
    for page in storage.pages():
        for item in page:
            digest.update(json.dumps(item, sort_keys=True, separators=(',', ':')).encode() + b'\n')
            count += 1
        if count and count % 10000 == 0:
            print('ARCHIVE_INVENTORY_PROGRESS', count, flush=True)
    return digest.hexdigest()


def crc_table():
    table = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
        table.append(value)
    return table


CRC_TABLE = crc_table()


def download_one(storage, root, item, cached):
    path = safe_path(root, item['name'])
    size = int(item['size'])
    if not str(item['generation']).isdigit() or not 0 <= size <= MAX_OBJECT_BYTES:
        raise RuntimeError('SOURCE_OBJECT_REQUIRES_REVIEW')
    if (cached and cached[0] == str(item['generation']) and cached[1] == size and
            path.is_file() and path.stat().st_size == size and file_sha(path) == cached[2]):
        return cached[2], True
    if not item.get('md5Hash') and not item.get('crc32c'):
        raise RuntimeError('SOURCE_CHECKSUM_MISSING')
    if shutil.disk_usage(root).free < DISK_RESERVE + size * WORKERS:
        raise RuntimeError('ARCHIVE_LOCAL_DISK_LOW; source retained; do not delete trading data')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / ('.theta-download-' + uuid.uuid4().hex)
    try:
        for attempt in range(4):
            sha, md5 = hashlib.sha256(), hashlib.md5()
            crc, received = 0xFFFFFFFF, 0
            try:
                with storage.media(item) as response, temporary.open('wb') as stream:
                    temporary.chmod(0o600)
                    for chunk in iter(lambda: response.read(256 * 1024), b''):
                        received += len(chunk)
                        if received > size:
                            raise RuntimeError('SOURCE_OBJECT_SIZE_MISMATCH')
                        stream.write(chunk)
                        sha.update(chunk)
                        md5.update(chunk)
                        if not item.get('md5Hash'):
                            for byte in chunk:
                                crc = CRC_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
                    stream.flush()
                    os.fsync(stream.fileno())
                break
            except (URLError, TimeoutError, ConnectionError, OSError, IncompleteRead) as exc:
                if isinstance(exc, OSError) and exc.errno in (28, 122):
                    raise RuntimeError('ARCHIVE_LOCAL_DISK_FULL; downloaded checkpoints retained') from None
                if attempt == 3:
                    raise RuntimeError('STORAGE_DOWNLOAD_INTERRUPTED; rerun resumes verified files') from None
                time.sleep(2**attempt)
        provider_checksum = (base64.b64encode(md5.digest()).decode() if item.get('md5Hash') else
            base64.b64encode((crc ^ 0xFFFFFFFF).to_bytes(4, 'big')).decode())
        if received != size or provider_checksum != (item.get('md5Hash') or item.get('crc32c')):
            raise RuntimeError('SOURCE_OBJECT_CHECKSUM_MISMATCH')
        os.replace(temporary, path)
        return sha.hexdigest(), False
    finally:
        temporary.unlink(missing_ok=True)


def mirror(storage, folder):
    root = folder / 'archive'
    root.mkdir(exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise RuntimeError('SYMLINK_IN_ARCHIVE')
    scan = uuid.uuid4().hex
    count = reused = total_bytes = 0
    started = time.monotonic()
    print('ARCHIVE_TRANSFER_MODE', WORKERS, 'workers; persistent HTTPS connections', flush=True)
    checkpoint = folder / 'archive-checkpoints.sqlite'
    if checkpoint.is_symlink():
        raise RuntimeError('UNSAFE_CHECKPOINT_PATH')
    with sqlite3.connect(checkpoint) as db, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        checkpoint.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS objects (name TEXT PRIMARY KEY, generation TEXT, size INTEGER, sha TEXT, scan TEXT)')
        for page in storage.pages():
            # At most one small page/four streaming downloads resident at once.
            pending = []
            for item in page:
                cached = db.execute('SELECT generation,size,sha FROM objects WHERE name=?', (item['name'],)).fetchone()
                pending.append((item, pool.submit(download_one, storage, root, item, cached)))
            for item, future in pending:
                sha, skipped = future.result()
                db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?)',
                    (item['name'], str(item['generation']), int(item['size']), sha, scan))
                # New downloads retain per-object durable checkpoints. Reused files
                # already have durable generation/size/SHA records; only their scan
                # marker changes, so commit those together at the page boundary.
                if not skipped:
                    db.commit()
                count += 1
                reused += int(skipped)
                total_bytes += int(item['size'])
                if count % 50 == 0:
                    elapsed = max(time.monotonic() - started, 0.001)
                    print('ARCHIVE_COPY_PROGRESS', count, 'verified;', reused, 'reused;', total_bytes,
                        'bytes;', round(count / elapsed, 1), 'files/s;',
                        time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), flush=True)
            db.commit()
        if not count:
            raise RuntimeError('EMPTY_SOURCE_ARCHIVE')
        # Stream the manifest; exclude old local objects no longer in the source.
        temporary = root / '.theta-manifest-new'
        with temporary.open('w') as stream:
            stream.write('{"source":"gs://' + BUCKET + '","objects":[')
            first = True
            for name, size, sha in db.execute('SELECT name,size,sha FROM objects WHERE scan=? ORDER BY name', (scan,)):
                if not first:
                    stream.write(',')
                json.dump({'path': name, 'bytes': size, 'sha256': sha}, stream)
                first = False
            stream.write(']}')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, root / 'migration-manifest.json')
    print('ARCHIVE_DOWNLOAD_VERIFIED', count, 'objects', total_bytes, 'bytes;', reused, 'reused', flush=True)
    return root
