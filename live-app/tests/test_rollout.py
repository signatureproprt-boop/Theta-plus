import importlib.util
from pathlib import Path
import urllib.error
import pytest

spec = importlib.util.spec_from_file_location('verify_rollout', Path(__file__).parents[1] / 'deploy/verify_rollout.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_retries_404_and_old_revision():
    now = [0]
    good = dict(status='ok', version='v', revision='new', execution='NO_BROKER_EXECUTION')
    responses = [urllib.error.HTTPError('http://test', 404, 'missing', {}, None), dict(good, revision='old'), good]
    def fetch(url, timeout):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    result = m.wait_for_health('http://test', 'v', 'new', fetch=fetch,
        clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0] + n))
    assert result == good and now[0] == 4


def test_wrong_identity_times_out():
    now = [0]
    with pytest.raises(RuntimeError, match='timed out'):
        m.wait_for_health('http://test', 'v', 'new', timeout=5, fetch=lambda *a: {},
            clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0] + n))
    assert now[0] == 5


def test_public_health_path_avoids_cloud_run_reserved_suffix(monkeypatch):
    import io
    seen = []
    class Response(io.StringIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    def open_url(url, timeout):
        seen.append(url)
        return Response('{}')
    monkeypatch.setattr(m.urllib.request, 'urlopen', open_url)
    assert m.fetch_health('https://example.test/', 2) == {}
    assert seen == ['https://example.test/health-check']


def test_workspace_smoke_requires_both_symbols_and_setup_evidence():
    seen=[]
    def fetch(url,symbol,timeout):
        seen.append(symbol)
        return dict(symbol=symbol,mode='READ_ONLY_RESEARCH',history={'candles':[]},chain={'contracts':[]},
                    setups=[{'setup':'opening_range'},{'setup':'trend_pullback'}])
    assert m.verify_workspaces('https://example.test',fetch=fetch)
    assert seen==['nifty','banknifty']
    with pytest.raises(RuntimeError,match='identity/schema'):
        m.verify_workspaces('https://example.test',fetch=lambda *args:{'status':'ok'})
    def unavailable(*args):raise urllib.error.HTTPError('https://example.test',500,'error',{},None)
    with pytest.raises(urllib.error.HTTPError):m.verify_workspaces('https://example.test',fetch=unavailable)
