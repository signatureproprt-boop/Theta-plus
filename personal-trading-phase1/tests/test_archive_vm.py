import base64
from contextlib import contextmanager
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).parents[1]/'deploy'


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.syspath_prepend(str(DEPLOY))
    spec = importlib.util.spec_from_file_location('vm_worker_test', DEPLOY/'archive_vm.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def installer(monkeypatch):
    monkeypatch.syspath_prepend(str(DEPLOY))
    spec = importlib.util.spec_from_file_location('vm_installer_test', DEPLOY/'install_archive_vm.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class Source:
    def __init__(self):
        self.objects = {'a': (b'first', '1'), 'b': (b'second', '2')}
        self.downloads = []
        self.fail = False

    def pages(self):
        for name, (raw, generation) in self.objects.items():
            yield [{'name': name, 'generation': generation, 'size': len(raw),
                    'md5Hash': base64.b64encode(hashlib.md5(raw).digest()).decode()}]

    def media(self, item):
        self.downloads.append(item['name'])
        if self.fail and item['name'] == 'b':
            raise RuntimeError('SIMULATED_DISCONNECT')
        return BytesIO(self.objects[item['name']][0])


def test_persistent_worker_recovers_and_reuses_verified_bytes(worker, tmp_path):
    expected = 'a'*32
    source = Source()
    source.fail = True
    worker.queue(tmp_path, expected)
    with pytest.raises(RuntimeError, match='SIMULATED_DISCONNECT'):
        worker.execute(tmp_path, source)
    assert worker.status(tmp_path, expected)['state'] == 'RETRYING'
    assert (tmp_path/'archive/a').read_bytes() == b'first'
    source.fail = False
    source.downloads.clear()
    worker.execute(tmp_path, source)
    assert source.downloads == ['b']
    assert worker.status(tmp_path, expected)['state'] == 'VERIFIED'
    source.downloads.clear()
    worker.execute(tmp_path, source)
    assert source.downloads == []
    # A new final-delta request must scan again and replace changed generations.
    source.objects['a'] = (b'updated', '3')
    worker.queue(tmp_path, 'b'*32)
    worker.execute(tmp_path, source)
    assert source.downloads == ['a']
    assert worker.status(tmp_path, expected)['state'] == 'PENDING'
    assert (tmp_path/'archive/a').read_bytes() == b'updated'


def test_no_stale_or_tampered_success_receipt(worker, tmp_path):
    worker.queue(tmp_path, 'a'*32)
    worker.execute(tmp_path, Source())
    (tmp_path/'archive/migration-manifest.json').write_text('{}')
    with pytest.raises(RuntimeError, match='MANIFEST_CHANGED'):
        worker.status(tmp_path, 'a'*32)


def test_copy_and_seed_cannot_overlap(worker, tmp_path):
    with worker.locked(tmp_path):
        with pytest.raises(RuntimeError, match='VM_ARCHIVE_BUSY'):
            worker.queue(tmp_path, 'a'*32)
    assert not (tmp_path/'request.json').exists()


@pytest.mark.parametrize('value', ['', '../bad', 'a'*31, 'A'*32, 'a'*32+';echo'])
def test_request_identifiers_are_bounded(worker, value):
    with pytest.raises(RuntimeError, match='INVALID_ARCHIVE_REQUEST'):
        worker.request_id(value)


def test_metadata_auth_refreshes_without_owner_login(worker, monkeypatch):
    requests = []
    clock = [100]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    class Reply(BytesIO):
        headers = {'Metadata-Flavor': 'Google'}
    def open_metadata(request, timeout):
        requests.append((request, timeout))
        return Reply(json.dumps({'access_token': 'private-'+str(len(requests)),
                                 'expires_in': 120}).encode())
    storage = worker.VMStorage()
    storage.metadata = SimpleNamespace(open=open_metadata)
    assert storage.access_token() == 'private-1'
    clock[0] = 150
    assert storage.access_token() == 'private-1'
    clock[0] = 191
    assert storage.access_token() == 'private-2'
    assert storage.access_token(force=True) == 'private-3'
    assert all(r.full_url == worker.METADATA and r.get_header('Metadata-flavor') == 'Google'
               and timeout == 10 for r, timeout in requests)
    assert worker.NoRedirect().redirect_request(None) is None


def test_metadata_failure_does_not_expose_tokens(worker, capsys):
    storage = worker.VMStorage()
    def fail(*args, **kwargs):
        raise ValueError('private-access-token-body')
    storage.metadata = SimpleNamespace(open=fail)
    with pytest.raises(RuntimeError, match='VM_METADATA_AUTH_UNAVAILABLE') as caught:
        storage.access_token()
    assert caught.value.__suppress_context__
    assert 'private-access-token' not in str(caught.value) + capsys.readouterr().out
    assert storage.token is None


@pytest.mark.parametrize('headers,token,expires', [({}, 'secret', 3600),
    ({'Metadata-Flavor': 'Google'}, '', 3600),
    ({'Metadata-Flavor': 'Google'}, 'secret', 0)])
def test_invalid_metadata_never_becomes_cached_credentials(worker, headers, token, expires):
    class Reply(BytesIO):
        pass
    reply = Reply(json.dumps({'access_token': token, 'expires_in': expires}).encode())
    reply.headers = headers
    storage = worker.VMStorage()
    storage.metadata = SimpleNamespace(open=lambda *a, **kw: reply)
    with pytest.raises(RuntimeError, match='VM_METADATA_AUTH_UNAVAILABLE'):
        storage.access_token()
    assert storage.token is None


def test_installer_refuses_live_handover_before_identity_changes(installer, monkeypatch):
    monkeypatch.setattr(installer, 'current_fence', lambda: ({'state': 'FENCED'}, '1'))
    monkeypatch.setattr(installer, 'urlopen', lambda *a, **kw: BytesIO(b'{"state":"LEGACY_BACKEND"}'))
    monkeypatch.setattr(installer, 'api_module', lambda *a, **kw: pytest.fail('Must fail before VM read'))
    with pytest.raises(RuntimeError, match='HANDOVER_STATE_REQUIRES_REVIEW'):
        installer.preflight()


def test_installer_refuses_new_live_owner_even_with_legacy_marker(installer, monkeypatch):
    monkeypatch.setattr(installer, 'current_fence', lambda: ({}, '0'))
    monkeypatch.setattr(installer, 'urlopen', lambda *a, **kw: BytesIO(b'{"state":"LEGACY_BACKEND"}'))
    monkeypatch.setattr(installer, 'api_module', lambda *a, **kw: '{"owner":{"state":"NEW_BACKEND"}}')
    with pytest.raises(RuntimeError, match='VM_LIVE_OWNER_REQUIRES_REVIEW'):
        installer.preflight()


def test_reader_scope_never_replaces_an_existing_identity(installer):
    assert installer.validate_accounts({}) is False
    good = {'email': installer.ACCOUNT, 'scopes': [installer.SCOPE]}
    assert installer.validate_accounts({'serviceAccounts': [good]}) is True
    for row in ({**good, 'email': 'other@project'}, {**good, 'scopes': ['cloud-platform']}):
        with pytest.raises(RuntimeError, match='EXISTING_VM_IDENTITY'):
            installer.validate_accounts({'serviceAccounts': [row]})


def test_reader_with_project_roles_cannot_be_attached(installer, monkeypatch):
    calls = []
    def cloud(*args, **kwargs):
        calls.append(args)
        if args[:3] == ('compute', 'instances', 'describe'):
            return '{}'
        if args[:3] == ('iam', 'service-accounts', 'list'):
            return '[]'
        if args[:2] == ('projects', 'get-iam-policy'):
            return json.dumps({'bindings': [{'role': 'roles/editor',
                'members': ['serviceAccount:'+installer.ACCOUNT]}]})
        pytest.fail('Unexpected mutation: '+str(args))
    monkeypatch.setattr(installer, 'cloud', cloud)
    with pytest.raises(RuntimeError, match='READER_HAS_PROJECT_ROLES'):
        installer.configure_identity()
    assert len(calls) == 3


def test_identity_setup_only_grants_bucket_read_and_restarts_once(installer, monkeypatch):
    calls = []
    def cloud(*args, **kwargs):
        calls.append(args)
        if args[:3] == ('compute', 'instances', 'describe'):
            return '{}'
        if args[:3] == ('iam', 'service-accounts', 'list'):
            return '[]'
        if args[:2] == ('projects', 'get-iam-policy'):
            return '{}'
        return ''
    monkeypatch.setattr(installer, 'cloud', cloud)
    monkeypatch.setattr(installer, 'preflight', lambda: None)
    monkeypatch.setattr(installer, 'ssh', lambda *a, **kw: '')
    installer.configure_identity()
    grants = [c for c in calls if 'add-iam-policy-binding' in c]
    assert len(grants) == 1
    assert grants[0][:4] == ('storage','buckets','add-iam-policy-binding',installer.BUCKET)
    assert '--role=roles/storage.objectViewer' in grants[0]
    assert '--member=serviceAccount:'+installer.ACCOUNT in grants[0]
    assert sum(c[:3] == ('compute','instances','stop') for c in calls) == 1
    attach = next(c for c in calls if c[:3] == ('compute','instances','set-service-account'))
    assert '--scopes=storage-ro' in attach
    assert calls[-1][:3] == ('compute','instances','start')


def test_vm_starts_again_if_identity_attachment_fails(installer, monkeypatch):
    calls = []
    def cloud(*args, **kwargs):
        calls.append(args)
        if args[:3] == ('compute','instances','describe'):
            return '{}'
        if args[:3] == ('iam','service-accounts','list'):
            return '[]'
        if args[:2] == ('projects','get-iam-policy'):
            return '{}'
        if args[:3] == ('compute','instances','set-service-account'):
            raise RuntimeError('DENIED')
        return ''
    monkeypatch.setattr(installer, 'cloud', cloud)
    monkeypatch.setattr(installer, 'preflight', lambda: None)
    with pytest.raises(RuntimeError, match='DENIED'):
        installer.configure_identity()
    assert calls[-1][:3] == ('compute','instances','start')


def test_new_final_request_is_reused_after_owner_reconnect(installer, monkeypatch):
    import merge_cloudshell as owner
    calls = []
    monkeypatch.setattr(owner, 'ssh', lambda *a, **kw: '{"version":1,"request":"'+('a'*32)+'"}')
    def remote(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('sudo', 'cat'):
            return json.dumps({'request': 'b'*32})
        if 'status' in args:
            assert args[-1] == 'b'*32
            return '{"state":"VERIFIED","manifest_sha256":"abc"}'
    monkeypatch.setattr(owner, 'remote', remote)
    assert owner.vm_archive() == '/srv/personal-trading/archive-worker/archive'
    assert not any('queue' in c for c in calls)


def test_owner_import_consumes_vm_archive_without_cloudshell_download(installer, monkeypatch, tmp_path):
    import merge_cloudshell as owner
    import archive_cloudshell as archive
    calls = []
    monkeypatch.setattr(owner, 'vm_archive', lambda **kw: '/srv/personal-trading/archive-worker/archive')
    monkeypatch.setattr(owner, 'inventory_digest', lambda: 'stable')
    monkeypatch.setattr(archive, 'mirror', lambda *a: pytest.fail('Must not download in Cloud Shell'))
    monkeypatch.setattr(owner, 'transfer_archive', lambda *a: pytest.fail('Must not transfer again'))
    monkeypatch.setattr(owner, 'remote', lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(owner, 'api_module', lambda *a, **kw: '{"state":"VERIFIED","objects":42}')
    assert owner.mirror_and_import(tmp_path, quiescent=True)['state'] == 'VERIFIED'
    assert calls[0][-1] == '/srv/personal-trading/archive-worker/archive'
