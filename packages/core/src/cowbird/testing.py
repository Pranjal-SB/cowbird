"""A provider that does not touch a network.

Shipped rather than kept in tests/: four tasks need it, one of them from a
different package, and anyone writing a provider wants it too.
"""

from __future__ import annotations

from datetime import timedelta

from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.provider import GenerateOptions, Provider

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

    def __init__(self, http: object = None, pages: object = ()) -> None:
        super().__init__(http)
        self.pages = list(pages)

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        return Address(value="a@fake.test", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        return self.pages.pop(0) if self.pages else []

    async def get(self, address: Address, id: str) -> Message:
        return Message(
            id=id, sender="s@x.test", subject="hi", received_at=None, html="", text=""
        )
