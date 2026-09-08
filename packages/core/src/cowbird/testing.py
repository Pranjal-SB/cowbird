"""A provider that does not touch a network.

Shipped rather than kept in tests/: four tasks need it, one of them from a
different package, and anyone writing a provider wants it too.

Also ships `provider_class`, `raising` and `build_pool` so the server tests
(and any provider package) can assemble a `Pool` of fakes without a network.
Being installed package code rather than a test fixture, they import cleanly
from any test module, sidestepping the `--import-mode=importlib` problem that
blocks `from tests.conftest import ...`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

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

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        return Address(value="a@fake.test", provider=self.name)

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
