from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from cowbird.pool import Pool
from cowbird.testing import build_pool, provider_class
from cowbird_server import create_app, db
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
    # Without this, a configured DATABASE_URL -- which is CI, and any developer
    # with the stack up -- silently turns roughly a hundred MemoryStore tests
    # into PostgresStore tests with a live flush loop, and lets a half-finished
    # pg test's quarantine row leak into an unrelated assertion here.
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
        monkeypatch.delenv("DATABASE_URL", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        with TestClient(create_app(pool=build_pool(*classes))) as client:
            yield client
        get_settings.cache_clear()

    return make


# The pg tests drop and truncate every table. .env.example and compose both
# point DATABASE_URL at the database a developer runs the stack against, so
# without this a plain `pytest` run takes the live tables out from under two
# running servers -- the same hazard as the health file above, through the
# database instead. Migrations build the tables inside this schema, and the
# destructive statements can never reach the real ones.
TEST_SCHEMA = "cowbird_test"


@pytest.fixture
def pg_schema() -> str:
    return TEST_SCHEMA


@pytest.fixture
def pg_pool():
    """Factory for pools scoped to the throwaway test schema.

    A factory rather than a pool because one test needs two of them, and
    because each pg module wants a different truncate before it yields. The
    caller closes what it opens, as it did before.
    """

    async def make():
        dsn = os.environ["DATABASE_URL"]
        await db.ensure_schema(dsn, TEST_SCHEMA)
        return await db.connect(dsn, schema=TEST_SCHEMA)

    return make
