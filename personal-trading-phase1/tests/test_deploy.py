import importlib.util
from pathlib import Path
from types import SimpleNamespace

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
