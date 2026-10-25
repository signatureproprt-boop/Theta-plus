import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

SPEC = importlib.util.spec_from_file_location('phase1_updater', Path(__file__).parents[1] / 'deploy/auto_update.py')
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def test_failed_integration_never_authorizes_deployment(monkeypatch):
    run = {'id': 1, 'path': '.github/workflows/personal-trading-phase1.yml', 'head_branch': 'main', 'event': 'push'}
    def get(path):
        if path.startswith('/actions/runs?'):
            return {'workflow_runs': [run]}
        return {'jobs': [{'name': updater.CHECK, 'conclusion': 'failure'},
                         {'name': 'Discover persistent deployment target (read-only)', 'conclusion': 'success'}]}
    monkeypatch.setattr(updater, 'get', get)
    assert updater.verified('a' * 40) is False


def test_verified_integration_is_required_even_when_cloud_inventory_is_blocked(monkeypatch):
    run = {'id': 1, 'path': '.github/workflows/personal-trading-phase1.yml', 'head_branch': 'main', 'event': 'push'}
    def get(path):
        if path.startswith('/actions/runs?'):
            return {'workflow_runs': [run]}
        return {'jobs': [{'name': updater.CHECK, 'conclusion': 'success'},
                         {'name': 'Discover persistent deployment target (read-only)', 'conclusion': 'failure'}]}
    monkeypatch.setattr(updater, 'get', get)
    assert updater.verified('a' * 40) is True


def test_pull_request_results_do_not_authorize_release(monkeypatch):
    run = {'id': 1, 'path': '.github/workflows/personal-trading-phase1.yml', 'head_branch': 'main', 'event': 'pull_request'}
    monkeypatch.setattr(updater, 'get', lambda _: {'workflow_runs': [run]})
    assert updater.verified('a' * 40) is False


def mock_readiness(monkeypatch, configured, *, wrong_image=False):
    calls = []
    def compose(release, sha, *args, **kwargs):
        calls.append(args)
        if args == ('config', '--services'):
            return '\n'.join(configured)
        if args[:2] == ('ps', '-q'):
            assert args[-1] in configured, 'An absent service must never be polled'
            return args[-1]+'-container'
        return ''
    def run(*args, **kwargs):
        if args[:3] == ('docker', 'image', 'inspect'):
            return 'sha256:expected'
        if args[:2] == ('docker', 'inspect'):
            return 'true sha256:'+('wrong' if wrong_image else 'expected')
        pytest.fail('Unexpected external command')
    monkeypatch.setattr(updater, 'compose', compose)
    monkeypatch.setattr(updater, 'run', run)
    monkeypatch.setattr(updater.time, 'sleep', lambda _: None)
    return calls


def test_legacy_rollback_readiness_does_not_require_nonexistent_live_service(monkeypatch):
    calls = mock_readiness(monkeypatch, ['postgres', 'redis', 'migrate', 'api', 'worker', 'dispatcher'])
    assert updater.healthy(Path('/old'), 'a'*40)
    assert ('ps', '-q', 'live') not in calls
    assert any(c[:3] == ('exec', '-T', 'worker') for c in calls)


def test_new_candidate_cannot_pass_without_live_service(monkeypatch):
    calls = mock_readiness(monkeypatch, ['api', 'worker', 'dispatcher'])
    with pytest.raises(RuntimeError, match='RELEASE_MISSING_REQUIRED_SERVICES: live'):
        updater.healthy(Path('/candidate'), 'b'*40, require_live=True)
    assert calls == [('config', '--services')]


def test_current_release_checks_all_four_services_and_their_images(monkeypatch):
    calls = mock_readiness(monkeypatch, ['api', 'worker', 'dispatcher', 'live'])
    assert updater.healthy(Path('/new'), 'b'*40, require_live=True)
    assert ('ps', '-q', 'live') in calls


def test_wrong_running_image_does_not_pass_readiness(monkeypatch):
    mock_readiness(monkeypatch, ['api', 'worker', 'dispatcher', 'live'], wrong_image=True)
    assert not updater.healthy(Path('/new'), 'b'*40, require_live=True)


def test_missing_base_service_is_never_treated_as_healthy_rollback(monkeypatch):
    mock_readiness(monkeypatch, ['api', 'worker'])
    with pytest.raises(RuntimeError, match='dispatcher'):
        updater.healthy(Path('/broken'), 'b'*40)
