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
