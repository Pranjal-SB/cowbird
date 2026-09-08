import os

import pytest
from cowbird.testing import build_pool, provider_class
from cowbird_server import create_app
from cowbird_server.config import get_settings
from cowbird_server.store import MemoryStore, StoreUnavailable
from fastapi.testclient import TestClient

KEY = "test-key"


class DeadStore:
    """A store whose backend is gone. Same protocol, always unavailable."""

    async def put(self, address):
        raise StoreUnavailable("connection refused")

    async def get(self, value):
        raise StoreUnavailable("connection refused")

    async def aclose(self):
        pass


def test_a_dead_store_is_a_503_not_a_500(monkeypatch, tmp_path):
    # 500 tells a client the request was wrong. A database that is briefly gone
    # is a transient dependency failure and the client should retry.
    monkeypatch.setenv("API_KEYS", KEY)
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    app = create_app(pool=build_pool(provider_class("fake")), store=DeadStore())
    with TestClient(app) as client:
        response = client.post("/v1/inboxes", headers={"x-api-key": KEY})
    get_settings.cache_clear()
    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    # Never the driver's message: it can carry a DSN, and that carries a password.
    assert "connection refused" not in body["error"]


def test_no_database_url_still_selects_memorystore(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", KEY)
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    app = create_app(pool=build_pool(provider_class("fake")))
    with TestClient(app):
        assert isinstance(app.state.store, MemoryStore)
        assert app.state.db_pool is None
    get_settings.cache_clear()


def test_an_unreachable_database_refuses_to_start(monkeypatch, tmp_path):
    # Falling back to MemoryStore while an operator believes state is shared is
    # worse than not coming up: the failure stays invisible until reads miss.
    monkeypatch.setenv("API_KEYS", KEY)
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    get_settings.cache_clear()
    app = create_app(pool=build_pool(provider_class("fake")))
    with pytest.raises(Exception):  # noqa: B017, SIM117 - asyncpg raises several types here
        with TestClient(app):
            pass
    get_settings.cache_clear()


@pytest.mark.pg
@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="no DATABASE_URL")
def test_database_url_selects_postgresstore(monkeypatch, tmp_path):
    from cowbird_server.pgstore import PostgresStore

    monkeypatch.setenv("API_KEYS", KEY)
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    app = create_app(pool=build_pool(provider_class("fake")))
    with TestClient(app):
        assert isinstance(app.state.store, PostgresStore)
    get_settings.cache_clear()


def test_a_single_instance_saves_its_health_on_shutdown(monkeypatch, tmp_path):
    # It had been seeding from this file and never writing it, so a
    # single-instance deploy discarded every measurement it took.
    import json

    path = tmp_path / "health.json"
    monkeypatch.setenv("API_KEYS", KEY)
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    app = create_app(pool=build_pool(provider_class("fake")))
    with TestClient(app) as client:
        client.post("/v1/inboxes", headers={"x-api-key": KEY})
    get_settings.cache_clear()
    assert "fake" in json.loads(path.read_text())
