from __future__ import annotations

from contextlib import asynccontextmanager

from cowbird.health import HealthStore, default_health_path
from cowbird.inbox import aclose_default_pool, default_pool
from cowbird.pool import Pool
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from cowbird_server.config import get_settings
from cowbird_server.envelope import envelope
from cowbird_server.routes import router

__all__ = ["create_app"]


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
        app.state.pool.health.seed(HealthStore.load(default_health_path()))
        yield
        if pool is None:
            await aclose_default_pool()

    app = FastAPI(title="cowbird", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return envelope(data={"status": "ok"})

    @app.exception_handler(HTTPException)
    async def _http_exception(request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content=envelope(error=exc.detail))

    app.include_router(router)

    return app
