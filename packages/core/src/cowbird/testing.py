"""A provider that does not touch a network.

Shipped rather than kept in tests/: four tasks need it, one of them from a
different package, and anyone writing a provider wants it too.

Also ships `provider_class`, `raising` and `build_pool` so the server tests
(and any provider package) can assemble a `Pool` of fakes without a network.
Being installed package code rather than a test fixture, they import cleanly
from any test module, sidestepping the `--import-mode=importlib` problem that
blocks `from tests.conftest import ...`.

`FakeTransport` replays recorded upstream responses for a provider's contract
suite, so every provider package tests against the same fake instead of carrying
its own.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlencode

from cowbird.errors import ProviderDown, RateLimited, SchemaDrift
from cowbird.health import HealthStore
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.pool import Pool
from cowbird.provider import GenerateOptions, Provider
from cowbird.registry import Registry
from cowbird.transport import Transport

CAPS = Capabilities(
    kind=frozenset({Kind.OWN_DOMAIN}),
    sites=("fake.test",),
    domains=("fake.test",),
    domain_count=1,
    address_ttl=None,
    message_ttl=timedelta(days=1),
    push=False,
    delete=False,
    custom_local=False,
    self_hosted=False,
    needs_residential_ip=False,
)


class FakeProvider(Provider):
    """Replays queued inbox pages. `pages` is consumed one entry per list() call;
    once exhausted, the inbox is empty forever."""

    name = "fake"
    caps = CAPS

    def __init__(
        self, http: Transport | None = None, pages: Sequence[list[MessageRow]] = ()
    ) -> None:
        # The fake never performs I/O, so unlike a real provider it can run
        # without a transport.
        super().__init__(http)  # type: ignore[arg-type]
        self.pages = list(pages)
        self._generated = 0

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        # a@fake.test first, as the CLI tests expect, then a1@, a2@...: a real
        # provider never hands two callers one inbox, and neither does the fake.
        n, self._generated = self._generated, self._generated + 1
        local = "a" if n == 0 else f"a{n}"
        return Address(value=f"{local}@fake.test", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        return self.pages.pop(0) if self.pages else []

    async def get(self, address: Address, id: str) -> Message:
        return Message(
            id=id, sender="s@x.test", subject="hi", received_at=None, html="", text=""
        )


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


@dataclass(frozen=True, slots=True)
class Reply:
    """A routed answer with its own status and response metadata."""

    status: int = 200
    payload: object = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


class Responses:
    """A route that answers differently on each call; the last answer repeats.

    Explicit rather than a plain list, because a list is already a valid JSON
    payload: mail.tm answers its list endpoints with bare arrays.

    Build one inside the test's own fixture, never in a module-level route
    dict: a shallow `dict(DEFAULT_ROUTES)` copy still shares this instance
    (and its call counter) across every test in the module, which makes the
    answer sequence order-dependent.
    """

    def __init__(self, *answers: object) -> None:
        if not answers:
            raise ValueError("Responses needs at least one answer")
        self._answers = list(answers)
        self._calls = 0

    def next(self) -> object:
        answer = self._answers[min(self._calls, len(self._answers) - 1)]
        self._calls += 1
        return answer


@dataclass
class FakeResponse:
    status_code: int
    text: str
    headers: dict[str, str]
    cookies: dict[str, str]


class _CaseInsensitiveHeaders(dict):
    """A dict that matches header names the way curl_cffi's real response does.

    Keeps `dict(...)`-style construction and equality working for existing
    tests, but reads and writes are case-insensitive on the key.
    """

    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__(headers)

    def __getitem__(self, key: str) -> str:
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return any(k.lower() == str(key).lower() for k in self.keys())

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class FakeTransport:
    """Replays recorded upstream responses in place of a `Transport`.

    A route key is matched as a substring of "METHOD url?query json-body",
    longest key first, so a single-message URL never gets the list fixture and a
    backend that multiplexes one URL on a query parameter or a GraphQL query can
    still be routed per call. `json()` and `text()` ignore status, as they only
    ever see a response `Transport` let through; route an Exception to model a
    failure. A route whose status is 429, or >= 500, raises `RateLimited` or
    `ProviderDown` — exactly what `Transport` raises for those statuses — from
    `json()`, `text()` and `send()` alike; other failures are modelled by
    routing an Exception. `send()` is where status, headers and cookies are
    visible.
    """

    def __init__(self, provider: str, routes: dict[str, object], status: int = 200) -> None:
        self.provider = provider
        self.routes = routes
        self.status = status
        self.seen: list[tuple[str, str, dict]] = []

    def _answer(self, method: str, url: str, kw: dict) -> Reply:
        self.seen.append((method, url, kw))
        query = urlencode(kw.get("params") or {})
        body = json.dumps(kw["json"]) if "json" in kw else ""
        cookies = urlencode(kw.get("cookies") or {})
        headers = json.dumps(kw.get("headers") or {})
        target = f"{method} {url}?{query} {body} {cookies} {headers}"
        for key in sorted(self.routes, key=len, reverse=True):
            if key in target:
                answer = self.routes[key]
                if isinstance(answer, Responses):
                    answer = answer.next()
                if isinstance(answer, Exception):
                    raise answer
                return answer if isinstance(answer, Reply) else Reply(self.status, answer)
        raise AssertionError(f"unexpected request: {target}")

    def _resolve(self, method: str, url: str, kw: dict) -> Reply:
        reply = self._answer(method, url, kw)
        if reply.status == 429:
            raise RateLimited(f"{self.provider}: HTTP 429")
        if reply.status >= 500:
            raise ProviderDown(f"{self.provider}: HTTP {reply.status}")
        return reply

    @staticmethod
    def _text(payload: object) -> str:
        if isinstance(payload, str):
            return payload
        if payload is None:
            return ""
        return json.dumps(payload)

    async def json(self, method: str, url: str, **kw) -> object:
        payload = self._resolve(method, url, kw).payload
        if payload is None:
            raise SchemaDrift(self.provider, expected="a JSON body", got="")
        if not isinstance(payload, str):
            return copy.deepcopy(payload)
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise SchemaDrift(self.provider, expected="a JSON body", got=payload[:200]) from exc

    async def text(self, method: str, url: str, **kw) -> str:
        return self._text(self._resolve(method, url, kw).payload)

    async def send(self, method: str, url: str, **kw) -> FakeResponse:
        reply = self._resolve(method, url, kw)
        return FakeResponse(
            reply.status,
            self._text(reply.payload),
            _CaseInsensitiveHeaders(reply.headers),
            dict(reply.cookies),
        )

    async def aclose(self) -> None:
        pass
