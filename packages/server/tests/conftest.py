from __future__ import annotations

from contextlib import contextmanager

import pytest
from cowbird.pool import Pool
from cowbird.testing import build_pool, provider_class
from cowbird_server import create_app
from cowbird_server.config import get_settings
from fastapi.testclient import TestClient

KEY = "test-key"


@pytest.fixture
def auth() -> dict[str, str]:
    return {"x-api-key": KEY}


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


@pytest.fixture
def client_for(monkeypatch, tmp_path):
    """Build a TestClient over a pool of fake providers, with optional env overrides.

        with client_for(provider_class("fake", list=one_page)) as client: ...
        with client_for(provider_class("fake"), WAIT_MAX="2") as client: ...

    Later tasks need a different pool per test, which a plain fixture cannot
    express, so this returns a context manager rather than a client.
    """

    @contextmanager
    def make(*classes, **env):
        monkeypatch.setenv("API_KEYS", KEY)
        monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        with TestClient(create_app(pool=build_pool(*classes))) as client:
            yield client
        get_settings.cache_clear()

    return make
