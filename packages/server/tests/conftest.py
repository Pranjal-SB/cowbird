from __future__ import annotations

import pytest
from cowbird.health import HealthStore
from cowbird.pool import Pool
from cowbird.registry import Registry
from cowbird.testing import CAPS, FakeProvider
from cowbird_server import create_app
from cowbird_server.config import get_settings
from fastapi.testclient import TestClient

KEY = "test-key"
AUTH = {"x-api-key": KEY}


def provider_class(name: str, **namespace):
    """A FakeProvider subclass under a chosen name, with any method overridden.

    Tests that need a provider to fail pass an override, for example:
        provider_class("bad", generate=raising(ProviderDown("boom")))
    """
    return type(name.upper(), (FakeProvider,), {"name": name, "caps": CAPS, **namespace})


def raising(exc: Exception):
    async def method(self, *args, **kwargs):
        raise exc

    return method


def build_pool(*classes) -> Pool:
    health = HealthStore()
    registry = Registry(discover=False, health=health, transport_factory=lambda n: None)
    for cls in classes:
        registry.register(cls)
    return Pool(registry, health)


@pytest.fixture
def fake_pool() -> Pool:
    return build_pool(provider_class("fake"))


@pytest.fixture
def client(fake_pool, monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEYS", KEY)
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    with TestClient(create_app(pool=fake_pool)) as test_client:
        yield test_client
    get_settings.cache_clear()
