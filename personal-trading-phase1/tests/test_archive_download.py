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
