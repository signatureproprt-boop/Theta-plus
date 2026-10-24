import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.gaps import finalized_end
from app.legacy_store import valid_path
from app.live_feed import SessionStarted


def test_finalized_gap_window_excludes_current_minute_and_settling_tail():
    now = datetime(2026, 10, 26, 3, 48, 42, tzinfo=timezone.utc)
    assert finalized_end(now) == now.replace(minute=47, second=0)


@pytest.mark.parametrize('path', ['../tokens', '/absolute', 'a//b', 'a/./b', 'a/../../b', 'a\\b'])
def test_archive_path_cannot_escape_source(path):
    with pytest.raises(ValueError): valid_path(path)


def test_archive_source_updates_cannot_overwrite_local_writes():
    from app.merge_archive import import_file
    from app.models import LegacyObject
    fake = SimpleNamespace(sha256='local', source_sha256='source')
    db = Mock();db.get.return_value = fake
    with pytest.raises(ValueError, match='LOCAL_ARCHIVE_CONFLICT'):
        import_file(db, 'records/example/source.json', b'new source')
    db.add.assert_not_called()


def test_activation_needs_explicit_legacy_stop_and_verified_import():
    from app.merge_archive import activate
    with pytest.raises(ValueError, match='CONFIRM_LEGACY'):
        activate(False)


def test_old_and_new_sources_are_in_single_image():
    from pathlib import Path
    import yaml
    root = Path(__file__).parents[1]
    docker = (root/'Dockerfile').read_text()
    compose = yaml.safe_load((root/'compose.yaml').read_text())
    assert 'COPY live-app /srv/legacy' in docker
    assert compose['services']['api']['build']['context'] == '..'
    assert compose['services']['live']['command'] == ['python','-m','app.live_runtime']
    assert compose['services']['api']['ports'][0].startswith('127.0.0.1:')
    assert 'dhan_client_id' not in compose['services']['api']['secrets']
    assert all(service['read_only'] for service in (compose['services']['api'],compose['services']['live']))


def test_live_api_auth_still_required_and_browser_socket_rejects_invalid_key(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from app.main import app
    from app import live_api
    client = TestClient(app)
    monkeypatch.setattr(live_api, 'read_secret', lambda _: 'k'*48)
    with client.websocket_connect('/live/ws') as ws:
        ws.send_json({'api_key':'wrong'})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008
    assert client.get('/live/status').status_code in (401,503)


def test_known_calendar_does_not_poll_closed_weekend_repeatedly():
    from app.gaps import calendar_end, expected_minutes
    from app.dhan import IST
    saturday = datetime(2026,10,24,15,0,tzinfo=IST)
    end=calendar_end(saturday,'NSE','EQUITY')
    assert end == datetime(2026,10,23,15,30,tzinfo=IST)
    start=datetime(2026,10,23,15,29,tzinfo=IST)
    assert expected_minutes(start,saturday,'NSE','EQUITY') == {start.astimezone(timezone.utc)}
    assert calendar_end(saturday,'BSE','EQUITY') == saturday
    assert expected_minutes(start,saturday,'BSE','EQUITY') is None


def test_endpoint_family_spacing_is_account_shared(monkeypatch):
    import fakeredis
    from app import rate_gate
    from app.config import Settings
    monkeypatch.setattr(rate_gate,'settings',lambda:Settings(dhan_min_spacing_ms=250))
    redis=fakeredis.FakeRedis()
    rate_gate.RateGate('same-account',redis).acquire('chain',6000)
    with pytest.raises(rate_gate.GateDeferred) as error:
        rate_gate.RateGate('same-account',redis).acquire('chain',6000)
    assert error.value.seconds >= 5
