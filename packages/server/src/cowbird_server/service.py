from __future__ import annotations

import asyncio
from contextlib import aclosing

from cowbird.inbox import Inbox
from cowbird.models import Address, Message
from cowbird.parsing import extract_otp
from cowbird.pool import Pool, Request

from cowbird_server.store import Store


class UnknownAddress(Exception):
    """This server has no record of that address.

    Distinct from any provider error: it means the address was never issued
    here, or it expired and was evicted. Routes turn it into a 404.
    """


class InboxService:
    """The logic the routes call. Routes own HTTP; this owns everything else."""

    def __init__(self, pool: Pool, store: Store) -> None:
        self._pool = pool
        self._store = store

    async def create(self, req: Request) -> Address:
        _provider, address = await self._pool.acquire(req)
        await self._store.put(address)
        return address

    async def inbox(self, value: str) -> Inbox:
        """Rebuild a live Inbox from a stored address.

        The provider is looked up by name from the registry, so the object that
        serves a request is not the one that issued the address. That is what
        makes an address usable from a separate process, and later from a
        separate backend.
        """
        address = await self._store.get(value)
        if address is None:
            raise UnknownAddress(value)
        return Inbox(self._pool.registry.get(address.provider), address)

    async def get_message(self, value: str, message_id: str) -> tuple[Message, str | None]:
        """A message plus its OTP, computed here rather than in the route --
        the same value `wait` already computes at this layer, so no route
        calls cowbird.parsing directly."""
        box = await self.inbox(value)
        message = await box.get(message_id)
        return message, extract_otp(message.text)

    async def wait(
        self,
        value: str,
        timeout: float,
        pattern: str | None = None,
        want_otp: bool = False,
    ) -> tuple[Message | None, str | None]:
        """Hold until mail arrives or the timeout expires.

        Returns (message, otp). Both None means nothing arrived in time, which
        is a normal answer and not an error. With want_otp, messages that carry
        no code are skipped rather than returned, because a caller waiting for
        an OTP does not want the newsletter that arrived first.
        """
        box = await self.inbox(value)

        async def first() -> tuple[Message, str | None]:
            # aclosing matters: without it the polling generator is left to the
            # garbage collector when wait_for cancels, which leaks a task per
            # abandoned request.
            async with aclosing(box.watch()) as stream:
                async for message in stream:
                    code = extract_otp(message.text, pattern)
                    if want_otp and not code:
                        continue
                    return message, code
            raise TimeoutError("the mail stream ended")

        try:
            return await asyncio.wait_for(first(), timeout)
        except TimeoutError:
            return None, None
