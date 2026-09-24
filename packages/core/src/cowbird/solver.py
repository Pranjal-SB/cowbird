"""Client for an external Cloudflare solver (github.com/B00H0O/cloudflare-solver).

The solver is a separate service that drives a real browser; cowbird only talks
to it over HTTP, and only when COWBIRD_SOLVER_URL is set. It answers two
questions: a Turnstile token for a sitekey, and the cookies and User-Agent that
get past an IUAM interstitial.

Both answers are bound to the IP and User-Agent that earned them, so the
solver and cowbird must leave through the same public IP. A proxy on the
Transport is forwarded to the solver for exactly that reason.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from curl_cffi.requests import AsyncSession

from cowbird.errors import SolverUnavailable

ENV = "COWBIRD_SOLVER_URL"


@dataclass(frozen=True, slots=True)
class Clearance:
    # repr=False: cf_clearance is a credential for this egress.
    cookies: dict[str, str] = field(repr=False)
    user_agent: str


def _cookies(header: str) -> dict[str, str]:
    pairs = (part.strip().partition("=") for part in header.split(";"))
    return {name: value for name, _, value in pairs if name}


class Solver:
    def __init__(self, url: str, *, proxy: str | None = None, timeout: float = 90.0) -> None:
        self.url = url.rstrip("/")
        self.proxy = proxy
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> Solver | None:
        value = os.environ.get(ENV, "").strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"{ENV} must be an http(s):// URL, got {value!r}")
        return cls(value)

    def with_proxy(self, proxy: str | None) -> Solver:
        return Solver(self.url, proxy=proxy, timeout=self.timeout)

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        # One session per call: solves are rare and slow, and a held session
        # would only add a close() to thread through every owner.
        try:
            async with AsyncSession(timeout=self.timeout) as session:
                resp = await session.post(f"{self.url}{path}", json=body)
        except Exception as exc:  # curl_cffi raises its own transport errors
            raise SolverUnavailable(f"solver {path}: {exc}") from exc
        if resp.status_code >= 300:
            raise SolverUnavailable(f"solver {path}: HTTP {resp.status_code}")
        try:
            return json.loads(resp.text)
        except ValueError as exc:
            raise SolverUnavailable(f"solver {path}: not JSON: {resp.text[:200]!r}") from exc

    def _body(self, **fields: str | None) -> dict[str, str]:
        body = {k: v for k, v in fields.items() if v is not None}
        if self.proxy:
            body["proxy"] = self.proxy
        return body

    async def turnstile(
        self, url: str, sitekey: str, *, action: str | None = None, cdata: str | None = None
    ) -> str:
        answer = await self._post(
            "/turnstile", self._body(url=url, sitekey=sitekey, action=action, cdata=cdata)
        )
        token = answer.get("token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or not token:
            raise SolverUnavailable(f"solver /turnstile gave no token: {answer!r}"[:300])
        return token

    async def clearance(self, url: str) -> Clearance:
        answer = await self._post("/iuam", self._body(url=url))
        headers = answer.get("headers") if isinstance(answer, dict) else None
        headers = headers if isinstance(headers, dict) else {}
        cookies = _cookies(str(headers.get("Cookie") or ""))
        user_agent = headers.get("User-Agent")
        if "cf_clearance" not in cookies or not isinstance(user_agent, str) or not user_agent:
            raise SolverUnavailable("solver /iuam gave no cf_clearance cookie and User-Agent")
        return Clearance(cookies=cookies, user_agent=user_agent)
