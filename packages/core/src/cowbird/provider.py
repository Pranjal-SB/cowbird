from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

from cowbird.errors import NotSupported
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.transport import Transport


@dataclass(frozen=True, slots=True)
class GenerateOptions:
    kind: Kind | None = None
    local: str | None = None
    domain: str | None = None


class Provider(ABC):
    """One disposable-mail backend.

    Subclasses hold protocol knowledge and nothing else: no session, no retry
    logic, no parsing. If a subclass needs more than about forty lines of
    non-domain code, core is missing something.
    """

    name: ClassVar[str]
    caps: ClassVar[Capabilities]

    def __init__(self, http: Transport | None = None) -> None:
        self.http = http

    @abstractmethod
    async def generate(self, opts: GenerateOptions | None = None) -> Address: ...

    @abstractmethod
    async def list(self, address: Address) -> list[MessageRow]:
        """Inbox headers. An empty inbox is [], never an exception."""

    @abstractmethod
    async def get(self, address: Address, id: str) -> Message: ...

    async def delete(self, address: Address, id: str) -> None:
        raise NotSupported(f"{self.name} cannot delete messages")

    async def watch(self, address: Address, poll: float | None = None) -> AsyncIterator[Message]:
        """New mail as it arrives.

        This default polls. Providers with push (DropMail's WebSocket) override
        it, and callers cannot tell which one they got — which is why this is on
        the protocol rather than in the pool.
        """
        interval = self.caps.poll_interval if poll is None else poll
        seen: set[str] = set()
        while True:
            for row in await self.list(address):
                if row.id in seen:
                    continue
                seen.add(row.id)
                if row.locked:
                    continue
                yield await self.get(address, row.id)
            await asyncio.sleep(interval)
