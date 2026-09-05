from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace

from cowbird.errors import CowbirdError
from cowbird.health import HealthStore
from cowbird.models import Address, Message, MessageRow
from cowbird.parsing import extract_otp
from cowbird.pool import Pool, Request
from cowbird.provider import Provider
from cowbird.registry import Registry

_DEFAULT_POOL: Pool | None = None


def default_pool() -> Pool:
    global _DEFAULT_POOL
    if _DEFAULT_POOL is None:
        health = HealthStore()
        # The registry is handed the same store the pool routes on, so every
        # provider call — not just generate() — feeds routing decisions.
        _DEFAULT_POOL = Pool(Registry(health=health), health)
    return _DEFAULT_POOL


class Inbox:
    """One address, bound to the provider that issued it.

    `otp()` and `link()` are here rather than left to the caller because that
    watch-and-parse loop is what nearly every consumer actually wants, and it
    would otherwise become the most-copied snippet in the project.
    """

    def __init__(self, provider: Provider, address: Address) -> None:
        self.provider = provider
        self.address = address
        self._issued: list[str] = []

    async def messages(self) -> list[MessageRow]:
        return await self.provider.list(self.address)

    async def get(self, id: str) -> Message:
        return await self.provider.get(self.address, id)

    async def delete(self, id: str) -> None:
        await self.provider.delete(self.address, id)

    async def watch(self, poll: float | None = None) -> AsyncIterator[Message]:
        async for message in self.provider.watch(self.address, poll=poll):
            self._issued.append(message.id)
            yield message

    async def _first(self, extract, timeout: float, poll: float):
        async def loop():
            async for message in self.watch(poll=poll):
                found = extract(message)
                if found:
                    return found
            return None

        try:
            return await asyncio.wait_for(loop(), timeout=timeout)
        except TimeoutError:
            raise TimeoutError(
                f"no matching mail for {self.address.value} within {timeout}s"
            ) from None

    async def otp(
        self, timeout: float = 120, pattern: str | None = None, poll: float | None = None
    ) -> str:
        return await self._first(lambda m: extract_otp(m.text, pattern), timeout, poll)

    async def link(
        self, timeout: float = 120, match: str | None = None, poll: float | None = None
    ) -> str:
        def pick(message: Message) -> str | None:
            for href in message.links:
                if match is None or match in href:
                    return href
            return None

        return await self._first(pick, timeout, poll)

    async def aclose(self) -> None:
        if not self.provider.caps.delete:
            return
        for id in self._issued:
            with suppress(CowbirdError):
                # Best-effort cleanup. The address expires on its own anyway,
                # and failing to tidy up must never fail the caller's work.
                await self.provider.delete(self.address, id)


@asynccontextmanager
async def open_inbox(pool: Pool | None = None, **kw) -> AsyncIterator[Inbox]:
    provider, address = await (pool or default_pool()).acquire(Request(**kw))
    box = Inbox(provider, address)
    try:
        yield box
    finally:
        await box.aclose()


@asynccontextmanager
async def open_inboxes(n: int, pool: Pool | None = None, **kw) -> AsyncIterator[list[Inbox]]:
    pool = pool or default_pool()
    req = Request(**kw)
    # Spread across distinct providers rather than hammering whichever one ranks
    # first: it is what the spec promises, it respects each backend's
    # concurrency budget, and it means one backend dying costs you 1/n of the
    # batch instead of all of it. Wraps around when n exceeds the candidates.
    names = [p.name for p in pool.candidates(req)] or [None]
    acquired = await asyncio.gather(
        *(
            pool.acquire(replace(req, provider=names[i % len(names)]))
            for i in range(n)
        )
    )
    boxes = [Inbox(p, a) for p, a in acquired]
    try:
        yield boxes
    finally:
        await asyncio.gather(*(b.aclose() for b in boxes))
