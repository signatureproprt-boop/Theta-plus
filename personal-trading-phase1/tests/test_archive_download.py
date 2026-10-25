import base64
from contextlib import contextmanager
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

SPEC = importlib.util.spec_from_file_location('archive_download', Path(__file__).parents[1]/'deploy/archive_cloudshell.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def item(name, raw, generation='1'):
    return {'name': name, 'generation': generation, 'size': str(len(raw)),
        'md5Hash': base64.b64encode(hashlib.md5(raw).digest()).decode()}


class FakeStorage:
    def __init__(self, objects):
        self.objects = objects
        self.downloads = []
        self.fail = None

    def pages(self):
        rows = [item(name, raw, generation) for name, (raw, generation) in sorted(self.objects.items())]
        for start in range(0, len(rows), 2):
            yield rows[start:start+2]

    def media(self, metadata):
        self.downloads.append(metadata['name'])
        if metadata['name'] == self.fail:
            raise RuntimeError('SIMULATED_INTERRUPTION')
        return BytesIO(self.objects[metadata['name']][0])


@pytest.fixture(autouse=True)
def enough_disk(monkeypatch):
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=80*1024**3))


def test_download_generation_resume_and_corrupt_cache_repair(tmp_path):
    source = FakeStorage({'a.json': (b'one', '1'), 'folder/b.json': (b'two', '2')})
    root = module.mirror(source, tmp_path)
    manifest = json.loads((root/'migration-manifest.json').read_text())
    assert len(manifest['objects']) == 2
    assert (root/'a.json').read_bytes() == b'one'
    source.downloads.clear()
    module.mirror(source, tmp_path)
    assert source.downloads == []
    (root/'a.json').write_bytes(b'bad')
    module.mirror(source, tmp_path)
    assert source.downloads == ['a.json']
    source.objects['folder/b.json'] = (b'new', '3')
    source.downloads.clear()
    module.mirror(source, tmp_path)
    assert source.downloads == ['folder/b.json']
    assert (root/'folder/b.json').read_bytes() == b'new'


def test_failed_download_retains_checkpoints_and_resumes(tmp_path):
    source = FakeStorage({'a': (b'first', '1'), 'b': (b'second', '1')})
    source.fail = 'b'
    with pytest.raises(RuntimeError, match='SIMULATED_INTERRUPTION'):
        module.mirror(source, tmp_path)
    assert not (tmp_path/'archive/migration-manifest.json').exists()
    source.fail = None
    source.downloads.clear()
    module.mirror(source, tmp_path)
    assert source.downloads == ['b']


def test_reused_records_commit_per_page_but_still_repair_corruption(tmp_path, monkeypatch):
    import sqlite3
    source = FakeStorage({'a': (b'first', '1'), 'b': (b'second', '1')})
    module.mirror(source, tmp_path)
    commits = []
    connect = sqlite3.connect
    class Connection(sqlite3.Connection):
        def commit(self):
            commits.append(1)
            return super().commit()
    monkeypatch.setattr(module.sqlite3, 'connect', lambda *a, **k: connect(*a, factory=Connection, **k))
    source.downloads.clear()
    module.mirror(source, tmp_path)
    assert source.downloads == [] and len(commits) == 1
    commits.clear()
    (tmp_path/'archive/a').write_bytes(b'wrong')
    module.mirror(source, tmp_path)
    assert source.downloads == ['a']
    assert (tmp_path/'archive/a').read_bytes() == b'first'
    assert len(commits) == 2  # Repaired object immediately; reused scan marker at page end.


def test_manifest_excludes_removed_source_without_deleting_local_archive(tmp_path):
    source = FakeStorage({'a': (b'keep', '1'), 'b': (b'old', '1')})
    root = module.mirror(source, tmp_path)
    del source.objects['b']
    module.mirror(source, tmp_path)
    assert (root/'b').read_bytes() == b'old'
    assert [r['path'] for r in json.loads((root/'migration-manifest.json').read_text())['objects']] == ['a']


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../b', 'a//b', 'a\\b', 'a/', 'migration-manifest.json'])
def test_unsafe_names_rejected(tmp_path, name):
    with pytest.raises(RuntimeError, match='UNSAFE_SOURCE'):
        module.safe_path(tmp_path, name)


def test_symlinks_rejected(tmp_path):
    (tmp_path/'folder').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(RuntimeError, match='SYMLINK'):
        module.safe_path(tmp_path, 'folder/private')


def test_checksum_mismatch_never_promotes_file(tmp_path):
    source = FakeStorage({'a': (b'wrong', '1')})
    with pytest.raises(RuntimeError, match='CHECKSUM_MISMATCH'):
        module.download_one(source, tmp_path, item('a', b'right'), None)
    assert list(tmp_path.iterdir()) == []


def test_composite_object_crc32c_verified(tmp_path):
    source = FakeStorage({'a': (b'123456789', '1')})
    metadata = {'name': 'a', 'generation': '1', 'size': '9', 'crc32c': '4waSgw=='}
    sha, reused = module.download_one(source, tmp_path, metadata, None)
    assert sha == hashlib.sha256(b'123456789').hexdigest() and not reused


def test_low_disk_stops_before_download(tmp_path, monkeypatch):
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10))
    source = FakeStorage({'a': (b'one', '1')})
    with pytest.raises(RuntimeError, match='DISK_LOW'):
        module.download_one(source, tmp_path, item('a', b'one'), None)
    assert source.downloads == []


def test_api_pagination_and_generation_pinning():
    storage = module.Storage(None)
    urls = []
    def opened(url, raw=False):
        urls.append((url, raw))
        query = parse_qs(urlsplit(url).query)
        if query.get('alt') == ['media']:
            return BytesIO(b'content')
        page = ({'items': [item('a', b'a')], 'nextPageToken': 'next+token'} if 'pageToken' not in query else
            {'items': [item('b', b'b')]})
        return BytesIO(json.dumps(page).encode())
    storage.open = opened
    assert [r['name'] for p in storage.pages() for r in p] == ['a', 'b']
    assert parse_qs(urlsplit(urls[1][0]).query)['pageToken'] == ['next+token']
    with storage.media(item('a/b +.json', b'content', '123')) as response:
        assert response.read() == b'content'
    assert '/o/a%2Fb%20%2B.json?' in urls[-1][0]
    assert parse_qs(urlsplit(urls[-1][0]).query)['generation'] == ['123']
    assert urls[-1][1] is True


def test_token_refresh_and_permission_failure_are_bounded(monkeypatch):
    tokens, requests = [], []
    def cloud(*args, **kwargs):
        tokens.append(args)
        return 'secret-token-' + str(len(tokens))
    def opened(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 401, 'unauthorized', {}, None)
        return BytesIO(b'ok')
    monkeypatch.setattr(module, 'urlopen', opened)
    storage = module.Storage(cloud)
    with storage.open('https://storage.googleapis.com/example') as response:
        assert response.read() == b'ok'
    assert len(tokens) == 2
    assert requests[-1].headers['Accept-encoding'] == 'identity'
    monkeypatch.setattr(module, 'urlopen', lambda *a, **k: (_ for _ in ()).throw(
        HTTPError('https://storage.googleapis.com/example', 403, 'secret-body', {}, None)))
    with pytest.raises(RuntimeError, match='^STORAGE_HTTP_403$'):
        storage.open('https://storage.googleapis.com/example')


def test_auth_failure_waits_and_recovers_without_restarting_scan(tmp_path, monkeypatch, capsys):
    import subprocess
    calls, sleeps = [], []
    def cloud(*args, **kwargs):
        calls.append(args)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(1, ['gcloud'], output='SECRET', stderr='SECRET')
        return 'recovered-secret-token'
    monkeypatch.setattr(module.time, 'sleep', sleeps.append)
    storage = module.Storage(cloud)
    source = FakeStorage({'a': (b'first', '1'), 'b': (b'second', '1')})
    def pages():
        yield [item('a', b'first')]
        assert (tmp_path/'archive/a').read_bytes() == b'first'
        assert storage.access_token(force=True) == 'recovered-secret-token'
        yield [item('b', b'second')]
    source.pages = pages
    module.mirror(source, tmp_path)
    assert source.downloads == ['a', 'b']
    assert sleeps == [30, 30]
    output = capsys.readouterr().out
    assert output.count('GOOGLE_AUTH_WAIT;') == 2
    assert 'GOOGLE_AUTH_RESUMED' in output
    assert 'SECRET' not in output and 'recovered-secret-token' not in output


def test_auth_wait_is_bounded_and_does_not_reuse_rejected_token(monkeypatch, capsys):
    import subprocess
    sleeps = []
    def cloud(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['gcloud'], stderr='SECRET')
    storage = module.Storage(cloud)
    storage.token = 'old-rejected-secret'
    monkeypatch.setattr(module.time, 'sleep', sleeps.append)
    with pytest.raises(RuntimeError, match='^GOOGLE_AUTH_WAIT_EXHAUSTED; verified files retained$'):
        storage.access_token(force=True)
    assert sleeps == [30] * 20
    assert 'SECRET' not in capsys.readouterr().out


def test_auth_recovery_is_shared_by_waiting_download_threads(monkeypatch):
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    calls, sleeps = [], []
    def cloud(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, ['gcloud'])
        return 'shared-token'
    monkeypatch.setattr(module.time, 'sleep', sleeps.append)
    storage = module.Storage(cloud)
    barrier = Barrier(4)
    def worker():
        barrier.wait(timeout=5)
        return storage.access_token()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker) for _ in range(4)]
        assert [f.result(timeout=5) for f in futures] == ['shared-token'] * 4
    assert len(calls) == 2 and sleeps == [30]


def test_inventory_detects_changed_generation_and_not_page_boundaries():
    source = FakeStorage({'a': (b'a', '1'), 'b': (b'b', '1')})
    first = module.inventory_digest(source)
    source.objects['b'] = (b'b', '2')
    assert module.inventory_digest(source) != first


def test_workspace_survives_exit_and_blocks_duplicate_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    with module.workspace() as folder:
        (folder/'checkpoint').write_text('retained')
        with pytest.raises(RuntimeError, match='MERGE_ALREADY_RUNNING'):
            with module.workspace():
                pass
    with module.workspace() as folder:
        assert (folder/'checkpoint').read_text() == 'retained'


def test_next_page_is_not_requested_before_previous_downloads_finish(tmp_path):
    source = FakeStorage({'a': (b'a', '1'), 'b': (b'b', '1')})
    def pages():
        yield [item('a', b'a')]
        assert (tmp_path/'archive/a').read_bytes() == b'a'
        yield [item('b', b'b')]
    source.pages = pages
    module.mirror(source, tmp_path)


def test_real_tar_stream_preserves_nested_archive_bytes(tmp_path, monkeypatch):
    import subprocess
    spec = importlib.util.spec_from_file_location('merge_transfer', Path(__file__).parents[1]/'deploy/merge_cloudshell.py')
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    mirror = tmp_path/'archive'
    (mirror/'nested').mkdir(parents=True)
    (mirror/'nested/source.bin').write_bytes(bytes(range(256))*100)
    (mirror/'migration-manifest.json').write_text('{"objects":[]}')
    target = tmp_path/'remote'
    target.mkdir()
    original = subprocess.Popen
    def receiver(args, **kwargs):
        if args[0] == 'gcloud':
            assert '--ssh-flag=-T' in args
            assert '--tunnel-through-iap' in args
            return original(['tar','-xzf','-','-C',str(target)], **kwargs)
        return original(args, **kwargs)
    monkeypatch.setattr(owner.subprocess, 'Popen', receiver)
    owner.transfer_archive(mirror)
    assert (target/'archive/nested/source.bin').read_bytes() == (mirror/'nested/source.bin').read_bytes()
    assert (target/'archive/migration-manifest.json').read_bytes() == (mirror/'migration-manifest.json').read_bytes()


def test_streamed_truncated_download_is_retried_from_start(tmp_path, monkeypatch):
    from http.client import IncompleteRead
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    source = FakeStorage({'a': (b'complete', '1')})
    attempts = []
    class Truncated(BytesIO):
        def read(self, *args):
            raise IncompleteRead(b'par', 8)
    def media(metadata):
        attempts.append(1)
        return Truncated(b'') if len(attempts) == 1 else BytesIO(b'complete')
    source.media = media
    module.download_one(source, tmp_path, item('a', b'complete'), None)
    assert len(attempts) == 2
    assert (tmp_path/'a').read_bytes() == b'complete'


def test_worker_reuses_media_connection_and_closes_partial_reads(monkeypatch):
    created = []
    class Response(BytesIO):
        status = 200
        def isclosed(self):
            return self.tell() == len(self.getvalue())
    class Connection:
        def __init__(self, host, **kwargs):
            assert host == 'storage.googleapis.com' and kwargs == {'timeout': 45}
            self.requests = []
            self.closed = False
            created.append(self)
        def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))
        def getresponse(self):
            return Response(b'body')
        def close(self):
            self.closed = True
    monkeypatch.setattr(module, 'HTTPSConnection', Connection)
    transport = module.MediaConnections()
    url = 'https://storage.googleapis.com/storage/v1/b/' + module.BUCKET + '/o/a?generation=1'
    for _ in range(3):
        with transport.open(url, {'Authorization':'Bearer test'}) as response:
            assert response.read() == b'body'
    assert len(created) == 1 and len(created[0].requests) == 3 and not created[0].closed
    with transport.open(url, {}) as response:
        assert response.read(1) == b'b'
    assert created[0].closed


@pytest.mark.parametrize('url', [
    'http://storage.googleapis.com/storage/v1/b/theta-pluse-theta-plus-data/o/a',
    'https://other.example/storage/v1/b/theta-pluse-theta-plus-data/o/a',
    'https://storage.googleapis.com/storage/v1/b/another-bucket/o/a',
    'https://storage.googleapis.com@other.example/storage/v1/b/theta-pluse-theta-plus-data/o/a',
])
def test_media_connection_never_sends_token_to_other_destination(url, monkeypatch):
    from unittest.mock import Mock
    opened = Mock()
    monkeypatch.setattr(module, 'HTTPSConnection', opened)
    with pytest.raises(RuntimeError, match='DESTINATION'):
        module.MediaConnections().open(url, {'Authorization':'Bearer test'})
    opened.assert_not_called()


def test_media_http_errors_retry_without_following_redirects(monkeypatch):
    from unittest.mock import Mock
    storage = module.Storage(lambda *a, **k: 'token')
    response = Mock(status=302)
    storage.media_connections.open = Mock(return_value=response)
    with pytest.raises(RuntimeError, match='^STORAGE_HTTP_302$'):
        storage.media(item('a', b'a'))
    response.close.assert_called_once()
    assert storage.media_connections.open.call_count == 1


def test_media_connection_is_thread_local(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    created = []
    class Connection:
        def __init__(self, *args, **kwargs):
            created.append(self)
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            from unittest.mock import Mock
            return Mock(status=200, isclosed=lambda: True)
    monkeypatch.setattr(module, 'HTTPSConnection', Connection)
    transport = module.MediaConnections()
    barrier = Barrier(2)
    def work():
        for _ in range(2):
            with transport.open('https://storage.googleapis.com/storage/v1/b/'+module.BUCKET+'/o/a', {}):
                pass
            barrier.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work) for _ in range(2)]
        for f in futures:
            f.result()
    assert len(created) == 2


def upgrade_module():
    spec = importlib.util.spec_from_file_location('upgrade_download', Path(__file__).parents[1]/'deploy/upgrade_archive_runner.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize('line,expected', [
    ('ARCHIVE_COPY_PROGRESS 400 verified; 200 reused', True),
    ('ARCHIVE_DATABASE_IMPORT_STARTED', False),
    ('MERGE_STAGE_2_FENCE_AND_STOP_PREVIOUS_COLLECTOR', False),
    ('ARCHIVE_DOWNLOAD_VERIFIED 400', False),
])
def test_upgrade_only_accepts_download_phase(tmp_path, line, expected):
    owner = upgrade_module()
    log = tmp_path/'log'
    log.write_text('earlier stage\n'+line+'\n')
    assert owner.download_phase(log) is expected


def test_upgrade_resumes_existing_process_if_handover_state_blocks(monkeypatch):
    owner = upgrade_module()
    signals = []
    monkeypatch.setattr(owner, 'has_children', lambda _: False)
    monkeypatch.setattr(owner, 'download_phase', lambda _: True)
    monkeypatch.setattr(owner, 'urlopen', lambda *a, **k: BytesIO(b'{"protocol":"unified-handover-v1","state":"FENCED"}'))
    monkeypatch.setattr(owner, 'signal_pid', lambda pid, kind: signals.append(kind))
    with pytest.raises(RuntimeError, match='HANDOVER_STATE'):
        owner.stop_download(123)
    assert signals == [owner.signal.SIGSTOP, owner.signal.SIGCONT]
    assert owner.signal.SIGTERM not in signals


def test_upgrade_stops_only_test_owned_download_and_keeps_checkpoint(tmp_path, monkeypatch):
    import subprocess
    import sys
    owner = upgrade_module()
    log = tmp_path/'log'
    log.write_text('ARCHIVE_COPY_PROGRESS 100 verified; 100 reused\n')
    checkpoint = tmp_path/'checkpoint'
    checkpoint.write_text('retain')
    monkeypatch.setattr(owner, 'LOG', log)
    # Some managed test runtimes expose child handles but hide their /proc view.
    monkeypatch.setattr(owner, 'has_children', lambda _: False)
    monkeypatch.setattr(owner, 'urlopen', lambda *a, **k: BytesIO(b'{"protocol":"unified-handover-v1","state":"LEGACY_BACKEND"}'))
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        owner.stop_download(child.pid)
        assert child.wait(timeout=5) == -owner.signal.SIGTERM
        assert checkpoint.read_text() == 'retain'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_upgrade_checks_children_of_every_download_thread(tmp_path):
    owner = upgrade_module()
    for thread in ('100', '101'):
        root = tmp_path/'100/task'/thread
        root.mkdir(parents=True)
        (root/'children').write_text('')
    assert not owner.has_children(100, tmp_path)
    (tmp_path/'100/task/101/children').write_text('200')
    assert owner.has_children(100, tmp_path)


def test_real_http_stream_reuses_connection_for_multiple_objects(monkeypatch):
    from http.client import HTTPConnection
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    connections = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def setup(self):
            super().setup()
            connections.append(self.connection)
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '4')
            self.end_headers()
            self.wfile.write(b'body')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    clients = []
    def client(*args, **kwargs):
        result = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        clients.append(result)
        return result
    monkeypatch.setattr(module, 'HTTPSConnection', client)
    storage = module.Storage(lambda *a, **k: 'test-only-token')
    try:
        for _ in range(4):
            with storage.media(item('a', b'body')) as response:
                assert response.read(2) == b'bo'
                assert response.read(2) == b'dy'
                assert response.read(2) == b''
        assert len(connections) == len(clients) == 1
    finally:
        for result in clients:
            result.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
