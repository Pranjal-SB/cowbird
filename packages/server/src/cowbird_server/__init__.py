from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from cowbird.errors import CowbirdError
from cowbird.health import HealthStore, default_health_path
from cowbird.inbox import aclose_default_pool, default_pool
from cowbird.pool import Pool
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException

from cowbird_server.auth import _key_matches
from cowbird_server.config import get_settings
from cowbird_server.envelope import envelope
from cowbird_server.errors import status_for
from cowbird_server.routes import router
from cowbird_server.service import InboxService, UnknownAddress
from cowbird_server.store import MemoryStore, Store
from cowbird_server.webhooks import WebhookManager

__all__ = ["create_app"]

logger = logging.getLogger("cowbird.server")


def _rate_key(request) -> str:
    # A *valid* API key gets its own budget, address-scoped, so one noisy
    # client cannot spend another's from behind the same NAT or edge worker.
    # An invalid or missing key buckets on the address alone: the header is
    # attacker controlled at this point (middleware runs before the auth
    # dependency), so if any header value bought its own bucket -- even paired
    # with the address -- rotating a bogus key each request would still mint
    # a fresh composite bucket per request and leave unauthenticated floods
    # unmetered, which is the bug this replaces. Checking it against the
    # configured keys here is cheap and needs no dependency injection.
    address = get_remote_address(request)
    key = request.headers.get("x-api-key", "")
    if key and _key_matches(key, get_settings().api_keys):
        return f"{address}|{key}"
    return address


def create_app(pool: Pool | None = None, store: Store | None = None) -> FastAPI:
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
        app.state.store = store if store is not None else MemoryStore()
        app.state.service = InboxService(app.state.pool, app.state.store)
        app.state.webhooks = WebhookManager(
            app.state.service,
            allow_private=settings.webhook_allow_private,
            secret=settings.webhook_secret,
        )
        yield
        await app.state.webhooks.shutdown()
        await app.state.store.aclose()
        if pool is None:
            await aclose_default_pool()

    app = FastAPI(title="cowbird", lifespan=lifespan)

    settings = get_settings()
    limiter = Limiter(key_func=_rate_key, default_limits=[settings.rate_limit])
    app.state.limiter = limiter

    @app.get("/health")
    @limiter.exempt
    async def health():
        return envelope(data={"status": "ok"})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request, exc: StarletteHTTPException):
        # Registered on Starlette's base class, not FastAPI's subclass:
        # Starlette's router raises the base class directly for 404/405, which
        # a handler registered on the FastAPI subclass never sees. FastAPI's
        # own HTTPException(...) call sites (auth included) still land here —
        # it's a subclass, and Starlette's handler lookup walks the MRO.
        return JSONResponse(status_code=exc.status_code, content=envelope(error=exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError):
        # Field locations only. FastAPI's default body echoes the caller's
        # submitted input back in `input`, which round-trips whatever they
        # sent (secrets included) straight into the error response.
        fields = ", ".join(".".join(str(part) for part in e["loc"]) for e in exc.errors())
        return JSONResponse(
            status_code=422, content=envelope(error=f"invalid request: {fields}")
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception(request, exc: Exception):
        # Never str(exc) here: an unhandled exception can carry anything,
        # including upstream response bodies. Log the real thing, return a
        # fixed message.
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content=envelope(error="internal server error"))

    @app.exception_handler(UnknownAddress)
    async def _unknown_address(request, exc: UnknownAddress):
        return JSONResponse(status_code=404, content=envelope(error="unknown address"))

    @app.exception_handler(CowbirdError)
    async def _cowbird_error(request, exc: CowbirdError):
        status, message = status_for(exc)
        # Full detail to the log, generic message to the caller.
        logger.warning("%s -> %s: %r", request.url.path, status, exc)
        headers = {"Retry-After": "30"} if status == 429 else None
        return JSONResponse(
            status_code=status, content=envelope(error=message), headers=headers
        )

    # Not app.include_router(router): this FastAPI wraps included routers in
    # a lazy _IncludedRouter with no .endpoint attribute, which is invisible
    # to slowapi's SlowAPIMiddleware (it walks app.routes looking for
    # hasattr(route, "endpoint") to decide what to rate-limit). router's
    # prefix and per-route auth dependency are already baked into each
    # APIRoute at decoration time, so splicing the routes in directly keeps
    # them real APIRoute objects the middleware can see.
    app.router.routes.extend(router.routes)

    app.add_middleware(SlowAPIMiddleware)

    @app.exception_handler(RateLimitExceeded)
    def _rate_limited(request, exc):
        # slowapi's SlowAPIMiddleware runs on BaseHTTPMiddleware, which calls
        # sync_check_limits internally; that helper refuses to await a
        # coroutine handler and silently falls back to slowapi's own
        # unenveloped default. Sync fixes it: no I/O here needed anyway.
        return JSONResponse(status_code=429, content=envelope(error="rate limit exceeded"))

    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    return app
