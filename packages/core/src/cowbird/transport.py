from __future__ import annotations

import asyncio
import json as jsonlib
import random
from typing import Any

from curl_cffi.requests import AsyncSession

from cowbird.errors import CloudflareChallenge, ProviderDown, RateLimited, SchemaDrift

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
    ) -> None:
        self.provider = provider
        self.impersonate = impersonate
        self.proxy = proxy
        self.timeout = timeout
        self.retries = retries
        # Held across every request to this backend, so no amount of caller
        # concurrency can exceed what the backend tolerates.
        self._gate = asyncio.Semaphore(max_concurrency)
        self._session: Any | None = None

    def _ensure_session(self) -> Any:
        if self._session is None:
            self._session = AsyncSession(
                impersonate=self.impersonate,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                timeout=self.timeout,
            )
        return self._session

    async def _request(self, method: str, url: str, **kw: Any) -> Any:
        async with self._gate:
            return await self._request_unthrottled(method, url, **kw)

    async def _request_unthrottled(self, method: str, url: str, **kw: Any) -> Any:
        session = self._ensure_session()
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await session.request(method, url, **kw)
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
            raise SchemaDrift(
                self.provider, expected="a JSON body", got=resp.text[:200]
            ) from exc

    async def text(self, method: str, url: str, **kw: Any) -> str:
        resp = await self._request(method, url, **kw)
        return resp.text

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
