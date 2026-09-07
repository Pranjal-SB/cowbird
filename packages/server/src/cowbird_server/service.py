from __future__ import annotations

from cowbird.inbox import Inbox
from cowbird.models import Address
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
