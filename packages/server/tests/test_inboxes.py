from __future__ import annotations

from datetime import UTC, datetime

from cowbird.health import HealthStore
from cowbird.models import MessageRow
from cowbird.pool import Pool
from cowbird.registry import Registry
from cowbird.testing import CAPS, FakeProvider
from cowbird_server import create_app
from cowbird_server.config import get_settings
from fastapi.testclient import TestClient

KEY = "test-key"
AUTH = {"x-api-key": KEY}


def provider_class(name: str, **namespace):
    """A FakeProvider subclass under a chosen name, with any method overridden."""
    return type(name.upper(), (FakeProvider,), {"name": name, "caps": CAPS, **namespace})


def build_pool(*classes) -> Pool:
    health = HealthStore()
    registry = Registry(discover=False, health=health, transport_factory=lambda n: None)
    for cls in classes:
        registry.register(cls)
    return Pool(registry, health)


def _client(pool, monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", "test-key")
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    return TestClient(create_app(pool=pool))


def test_creating_an_inbox_returns_the_address_and_its_provider(client):
    response = client.post("/v1/inboxes", headers=AUTH, json={})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["address"] == "a@fake.test"
    assert data["provider"] == "fake"


def test_creating_an_inbox_never_returns_the_provider_state(client):
    # state is a credential. It is held server-side precisely so it does not
    # travel, and a response that carries it defeats the whole store.
    response = client.post("/v1/inboxes", headers=AUTH, json={})
    assert "state" not in response.json()["data"]


def test_an_empty_inbox_lists_as_an_empty_array(client):
    client.post("/v1/inboxes", headers=AUTH, json={})
    response = client.get("/v1/inboxes/a@fake.test/messages", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": [], "error": None}


def test_listing_an_address_this_server_never_issued_is_a_404(client):
    response = client.get("/v1/inboxes/stranger@fake.test/messages", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["success"] is False


def test_messages_are_listed_with_their_headers(monkeypatch, tmp_path):
    when = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    rows = [MessageRow(id="1", sender="s@x.test", subject="hi", received_at=when, locked=True)]

    async def one_page(self, address):
        return rows

    with _client(build_pool(provider_class("fake", list=one_page)), monkeypatch, tmp_path) as c:
        c.post("/v1/inboxes", headers=AUTH, json={})
        data = c.get("/v1/inboxes/a@fake.test/messages", headers=AUTH).json()["data"]
    get_settings.cache_clear()
    assert data == [
        {
            "id": "1",
            "sender": "s@x.test",
            "subject": "hi",
            "received_at": "2026-09-07T12:00:00+00:00",
            "locked": True,
        }
    ]


def test_a_capability_request_is_passed_through_to_routing(client):
    # `kind` must reach Pool.candidates, or every capability filter the library
    # offers is silently unavailable over HTTP. The fake provider serves
    # own-domain only, so nothing satisfies gmail-alias and Pool.acquire raises
    # NoProviderAvailable.
    response = client.post("/v1/inboxes", headers=AUTH, json={"kind": "gmail-alias"})
    assert response.status_code == 503
