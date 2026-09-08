import pytest
from cowbird_server import create_app
from cowbird_server.config import get_settings
from fastapi.testclient import TestClient


def test_health_is_unauthenticated_and_enveloped(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", "k1")
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"status": "ok"}, "error": None}


def test_the_server_refuses_to_start_with_no_api_keys(monkeypatch, tmp_path):
    # An API with auth configured to accept nothing is worse than an API with
    # no auth: it looks protected. Fail at boot, loudly, not per request.
    monkeypatch.setenv("API_KEYS", "")
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="API_KEYS"), TestClient(create_app()):
        pass
