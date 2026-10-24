from unittest.mock import Mock
import pytest
from provider import Dhan, ProviderError
from worker import launch_worker


def test_migration_fence_blocks_provider_before_reading_credentials(monkeypatch):
    monkeypatch.delenv('THETA_EMBEDDED', raising=False)
    store=Mock();store.document.return_value=({'state':'FENCED'},1)
    creds=Mock()
    provider=Dhan(credentials=creds,store=store)
    with pytest.raises(ProviderError) as error:provider._request('charts/intraday',{})
    assert error.value.code=='MIGRATED'
    creds.token.assert_not_called()


def test_migrated_store_cannot_dispatch_old_job():
    store=Mock();store.document.return_value=({'state':'NEW_BACKEND'},1)
    assert launch_worker(store)=={'mode':'unified_backend','state':'migrated'}
