from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from cowbird.health import HealthStore
from cowbird.inbox import aclose_default_pool, default_pool
from cowbird.pool import Pool
from fastapi import FastAPI

from cowbird_server.config import get_settings
from cowbird_server.envelope import envelope

__all__ = ["create_app"]


def health_path() -> Path:
    override = os.environ.get("COWBIRD_HEALTH_PATH")
    if override:
        return Path(override)
    return Path.home() / ".cowbird" / "health.json"


def create_app(pool: Pool | None = None) -> FastAPI:
    """Build the app. `pool` is injectable so tests can hand in a fake registry
    and never touch a network; production passes nothing and gets the real one.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        if not settings.api_keys:
            raise RuntimeError("API_KEYS is empty; refusing to start")
        app.state.pool = pool if pool is not None else default_pool()
        # Seed the live store the registry already holds rather than replacing
        # it, so health measured this run lands where routing can see it.
        for name, entry in HealthStore.load(health_path()).snapshot().items():
            app.state.pool.health._entries[name] = entry
        yield
        if pool is None:
            await aclose_default_pool()

    app = FastAPI(title="cowbird", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return envelope(data={"status": "ok"})

    return app
