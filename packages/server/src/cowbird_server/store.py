from __future__ import annotations

from typing import Protocol, runtime_checkable

from cowbird.models import Address


@runtime_checkable
class Store(Protocol):
    """Where the server keeps the addresses it has issued.

    An address carries provider state (mail.tm's JWT, for one) that later calls
    need in order to authenticate. Handing that blob to the client and demanding
    it back on every request would work, and would make
    `GET /v1/inboxes/{addr}/messages` impossible to call from a plain curl. So
    the server holds it.

    Two implementations: MemoryStore here, and a Postgres-backed one once the
    fleet runs more than one instance. The second exists because the fan-out
    topology needs backend 2 to serve an address backend 1 issued, which no
    per-process dict can do.
    """

    async def put(self, address: Address) -> None: ...

    async def get(self, value: str) -> Address | None: ...

    async def aclose(self) -> None: ...


class MemoryStore:
    """One process, no durability. Live inboxes are lost on restart.

    That is acceptable for a single-instance deploy and is exactly what stops
    being acceptable the moment the worker fans out to a second backend.
    """

    def __init__(self) -> None:
        self._rows: dict[str, Address] = {}

    async def put(self, address: Address) -> None:
        self._rows[address.value] = address

    async def get(self, value: str) -> Address | None:
        address = self._rows.get(value)
        if address is None:
            return None
        if address.is_expired():
            del self._rows[value]
            return None
        return address

    async def aclose(self) -> None:
        # No connection to close. A Postgres-backed store closes its pool here.
        pass

    def size(self) -> int:
        """Row count including expired rows not yet read. Tests and diagnostics
        only; not part of the Store protocol."""
        return len(self._rows)
