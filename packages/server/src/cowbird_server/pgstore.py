"""The address store that more than one instance can read.

Same protocol as MemoryStore. Nothing in routes or services changes: if the
seam was drawn in the right place this drops in behind it, and if it was not,
this is where that shows.
"""

from __future__ import annotations

import logging

import asyncpg
from asyncpg.exceptions import InterfaceError
from cowbird.models import Address

from cowbird_server.store import StoreUnavailable

logger = logging.getLogger("cowbird.server")

# A closed or broken pool raises InterfaceError, which is not a PostgresError,
# so catching only the latter would let the most likely failure through
# unwrapped and reach the catch-all handler as a 500.
_DB_ERRORS = (asyncpg.PostgresError, OSError, InterfaceError)


class PostgresStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def put(self, address: Address) -> None:
        # Upsert, not insert. Two instances writing the same address is ordinary
        # (one issues it, another refreshes provider state), and a duplicate-key
        # error would surface as a 500 on a normal request.
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "insert into addresses (value, provider, state, expires_at)"
                    " values ($1, $2, $3, $4)"
                    " on conflict (value) do update set"
                    " provider = excluded.provider,"
                    " state = excluded.state,"
                    " expires_at = excluded.expires_at",
                    address.value,
                    address.provider,
                    address.state,
                    address.expires_at,
                )
        except _DB_ERRORS as exc:
            raise StoreUnavailable(str(exc)) from exc

    async def get(self, value: str) -> Address | None:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "select value, provider, state, expires_at from addresses"
                    " where value = $1",
                    value,
                )
                if row is None:
                    return None
                address = Address(
                    value=row["value"],
                    provider=row["provider"],
                    expires_at=row["expires_at"],
                    state=row["state"],
                )
                if address.is_expired():
                    # Deleted, not hidden. A store that answers None forever
                    # while holding the row is a leak with a polite face.
                    await conn.execute("delete from addresses where value = $1", value)
                    return None
                return address
        except _DB_ERRORS as exc:
            raise StoreUnavailable(str(exc)) from exc

    async def sweep(self) -> int:
        """Delete expired rows nobody read. Returns how many went.

        `get` evicts on read, which only ever reaches rows someone asks for.
        This is the rest of them, and it is the leak MemoryStore still has.
        """
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "delete from addresses where expires_at is not null and expires_at <= now()"
                )
            return int(result.split()[-1])
        except _DB_ERRORS as exc:
            raise StoreUnavailable(str(exc)) from exc

    async def aclose(self) -> None:
        # Best-effort, unlike the other three methods. This runs during lifespan
        # shutdown: there is no request to answer and nothing for a client to
        # retry, so raising StoreUnavailable here would only mask whatever else
        # is already tearing the server down. MemoryStore.aclose cannot fail
        # either, and the protocol should mean the same thing for both.
        try:
            await self._pool.close()
        except _DB_ERRORS as exc:
            logger.warning("error closing store pool: %s", exc)
