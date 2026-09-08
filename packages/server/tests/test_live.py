import pytest
from cowbird_server import create_app
from cowbird_server.config import get_settings
from fastapi.testclient import TestClient

pytestmark = pytest.mark.live


def test_the_real_fleet_answers_through_http(monkeypatch, tmp_path):
    """Acquire a real address over HTTP and list it.

    Everything else in this package runs against a fake pool, which proves the
    routes are wired correctly and proves nothing about whether a real provider
    can be reached through them.
    """
    monkeypatch.setenv("API_KEYS", "live-test-key")
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    auth = {"x-api-key": "live-test-key"}
    with TestClient(create_app()) as client:
        created = client.post("/v1/inboxes", headers=auth, json={})
        assert created.status_code == 200, created.text
        address = created.json()["data"]["address"]
        assert "@" in address

        listed = client.get(f"/v1/inboxes/{address}/messages", headers=auth)
        assert listed.status_code == 200, listed.text
        assert isinstance(listed.json()["data"], list)

        matrix = client.get("/v1/providers", headers=auth).json()["data"]
        assert {row["provider"] for row in matrix} >= {"mailtm", "inboxes", "emailnator"}
    get_settings.cache_clear()
