from __future__ import annotations

import asyncio
import json as jsonlib
import random
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from cowbird.errors import (
    CloudflareChallenge,
    ProviderDown,
    RateLimited,
    SchemaDrift,
    SolverUnavailable,
)

if TYPE_CHECKING:
    from cowbird.solver import Clearance, Solver

# Cloudflare's interstitial is an HTML page served with a 403 or 503. These are
# the stable markers across its variants.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "__cf_chl",
)


class Transport:
    """The only thing in Cowbird that touches a network.

    Providers receive one of these and never build a session themselves, so
    impersonation, proxying, retries, and anti-bot detection are fixed in one
    place for every provider at once.

    With `fresh_session`, a handshake spanning more than one request cannot
    work through this transport — request two has no cookie from request
    one — so such a backend has to carry its identity explicitly instead.
    """

    def __init__(
        self,
        provider: str,
        *,
        impersonate: str = "chrome",
        proxy: str | None = None,
        timeout: float = 60.0,
        retries: int = 2,
        max_concurrency: int = 4,
        fresh_session: bool = False,
        solver: Solver | None = None,
    ) -> None:
        self.provider = provider
        self.impersonate = impersonate
        self.proxy = proxy
        self.timeout = timeout
        self.retries = retries
        self.fresh_session = fresh_session
        # Held across every request to this backend, so no amount of caller
        # concurrency can exceed what the backend tolerates.
        self._gate = asyncio.Semaphore(max_concurrency)
        self._session: Any | None = None
        # A clearance is bound to the egress, so the solver must leave through
        # this transport's proxy too.
        self.solver = solver.with_proxy(proxy) if solver is not None and proxy else solver
        # Keyed by lowercased hostname. One clearance serves every address on
        # a host: it certifies the egress, not the inbox.
        self._clearances: dict[str, Clearance] = {}
        self._clearance_locks: dict[str, asyncio.Lock] = {}

    def _new_session(self) -> Any:
        return AsyncSession(
            impersonate=self.impersonate,
            proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
            timeout=self.timeout,
        )

    def _ensure_session(self) -> Any:
        if self._session is None:
            self._session = self._new_session()
        return self._session

    async def _send_once(self, method: str, url: str, **kw: Any) -> Any:
        if not self.fresh_session:
            return await self._ensure_session().request(method, url, **kw)
        # one TLS handshake per request. Pool sessions keyed by
        # identity if latency on these backends ever matters.
        session = self._new_session()
        try:
            return await session.request(method, url, **kw)
        finally:
            await session.close()

    async def _request(self, method: str, url: str, **kw: Any) -> Any:
        async with self._gate:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            used = self._clearances.get(host)
            try:
                return await self._request_unthrottled(method, url, **_cleared(kw, used))
            except CloudflareChallenge as challenge:
                if self.solver is None:
                    raise
                fresh = await self._refresh_clearance(parts.scheme, host, used, challenge)
            try:
                return await self._request_unthrottled(method, url, **_cleared(kw, fresh))
            except CloudflareChallenge:
                # Solved and still challenged: the clearance does not work from
                # here. At most one solve per request; no loop.
                if self._clearances.get(host) is fresh:
                    del self._clearances[host]
                raise

    async def _refresh_clearance(
        self, scheme: str, host: str, used: Clearance | None, challenge: Exception
    ) -> Clearance:
        lock = self._clearance_locks.setdefault(host, asyncio.Lock())
        async with lock:
            current = self._clearances.get(host)
            if current is not None and current is not used:
                return current  # a concurrent request already re-solved
            self._clearances.pop(host, None)
            # The origin, not the request URL: the solver's browser must not
            # GET a POST-only API route.
            try:
                fresh = await self.solver.clearance(f"{scheme}://{host}/")
            except SolverUnavailable as exc:
                raise exc from challenge
            self._clearances[host] = fresh
            return fresh

    async def _request_unthrottled(self, method: str, url: str, **kw: Any) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await self._send_once(method, url, **kw)
            except Exception as exc:  # curl_cffi raises its own transport errors
                last = ProviderDown(f"{self.provider}: {exc}")
            else:
                if self._is_challenge(resp):
                    raise CloudflareChallenge(
                        f"{self.provider}: anti-bot challenge (HTTP {resp.status_code})"
                    )
                if resp.status_code == 429:
                    raise RateLimited(f"{self.provider}: HTTP 429")
                if resp.status_code < 500:
                    return resp
                last = ProviderDown(f"{self.provider}: HTTP {resp.status_code}")

            if attempt < self.retries:
                await asyncio.sleep((2**attempt) * 0.25 + random.uniform(0, 0.25))
        raise last or ProviderDown(f"{self.provider}: request failed")

    @staticmethod
    def _is_challenge(resp: Any) -> bool:
        if resp.status_code not in (403, 503):
            return False
        if "cf-mitigated" in {k.lower() for k in resp.headers}:
            return True
        body = resp.text[:2000].lower()
        return any(marker in body for marker in _CHALLENGE_MARKERS)

    async def json(self, method: str, url: str, **kw: Any) -> Any:
        # Browser impersonation makes every request look like a browser, and a
        # browser's default Accept header content-negotiates: some APIs will
        # hand back HTML or XML instead of JSON to a client that looks like a
        # browser. Pinning Accept: application/json makes every backend's
        # response shape deterministic regardless of impersonation. An
        # explicit caller-supplied Accept still wins (merged in second).
        headers = {"Accept": "application/json", **kw.pop("headers", {})}
        resp = await self._request(method, url, headers=headers, **kw)
        try:
            return jsonlib.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.provider, expected="a JSON body", got=resp.text[:200]) from exc

    async def text(self, method: str, url: str, **kw: Any) -> str:
        resp = await self._request(method, url, **kw)
        return resp.text

    async def send(self, method: str, url: str, **kw: Any) -> Any:
        """Like json()/text(), but hands back the raw response instead of a
        parsed body, so an adapter can branch on `resp.status_code`.

        Goes through the same `_request` path as json()/text() — impersonation,
        the concurrency gate, retries, and challenge detection all still apply.
        Use this only when an adapter must distinguish response codes itself
        (e.g. a provider-specific 404 that means "empty inbox" vs. a 401 that
        means "bad credentials", or telling a successful empty-body 204 apart
        from a failed request). The caller is responsible for interpreting any
        4xx status; json()/text() remain the normal path for everything else.
        """
        headers = {"Accept": "application/json", **kw.pop("headers", {})}
        return await self._request(method, url, headers=headers, **kw)

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _cleared(kw: dict[str, Any], clearance: Clearance | None) -> dict[str, Any]:
    """The request kwargs with the clearance merged in, as a new dict. Its
    User-Agent wins over the caller's: the cookie is void under any other."""
    if clearance is None:
        return kw
    return {
        **kw,
        "cookies": {**(kw.get("cookies") or {}), **clearance.cookies},
        "headers": {**(kw.get("headers") or {}), "User-Agent": clearance.user_agent},
    }
