"""Health across instances.

Two tables, because health holds two kinds of fact. Latency and
needs_residential_ip belong to one instance's network path: fan-out exists to
have several different egress IPs, so a Cloudflare challenge served to one
backend says nothing about another, and merging them would let a blocked
instance mark a provider down for instances that can still reach it.
Quarantine is the exception. SchemaDrift means the adapter's parsing is wrong,
which is true of the code everywhere it runs.

There is no locking on either path. provider_health rows have exactly one
writer each because instance_id is in the primary key, and provider_quarantine
is insert-only with on-conflict-do-nothing.
"""

from __future__ import annotations

import logging

import asyncpg
from cowbird.health import HealthStore, Status

logger = logging.getLogger("cowbird.server")


class HealthSync:
    def __init__(self, pool: asyncpg.Pool, health: HealthStore, instance_id: str) -> None:
        self._pool = pool
        self._health = health
        self._instance = instance_id

    async def load(self) -> None:
        """Seed at startup: this instance's own rows, then the global quarantines.

        Raises. A failure here happens during the lifespan, where refusing to
        start is the right answer; `flush` is the one that must never raise.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "select provider, status, latencies, last_checked, last_failure,"
                " needs_residential_ip from provider_health where instance_id = $1",
                self._instance,
            )
            for row in rows:
                # restore() and not seed(): it applies the rule that a stale
                # DOWN degrades to OK, and it is the only path that does.
                self._health.restore(
                    row["provider"],
                    status=Status(row["status"]),
                    latencies=list(row["latencies"]),
                    last_checked=row["last_checked"],
                    last_failure=row["last_failure"],
                    needs_residential_ip=row["needs_residential_ip"],
                )
            await self._apply_quarantines(conn)

    async def flush(self) -> None:
        """Write this instance's rows, publish its quarantines, read back the
        global ones. Never raises: see the module docstring."""
        try:
            async with self._pool.acquire() as conn:
                snapshot = self._health.snapshot()
                for provider, entry in snapshot.items():
                    await conn.execute(
                        "insert into provider_health (instance_id, provider, status,"
                        " latencies, last_checked, last_failure, needs_residential_ip)"
                        " values ($1, $2, $3, $4, $5, $6, $7)"
                        " on conflict (instance_id, provider) do update set"
                        " status = excluded.status,"
                        " latencies = excluded.latencies,"
                        " last_checked = excluded.last_checked,"
                        " last_failure = excluded.last_failure,"
                        " needs_residential_ip = excluded.needs_residential_ip",
                        self._instance,
                        provider,
                        str(entry.status),
                        list(entry.latencies),
                        entry.last_checked,
                        entry.last_failure,
                        entry.needs_residential_ip,
                    )
                    if entry.status is Status.QUARANTINED:
                        # First writer wins. A later instance noticing the same
                        # drift must not overwrite who saw it first or when.
                        await conn.execute(
                            "insert into provider_quarantine (provider, reason, instance_id)"
                            " values ($1, $2, $3) on conflict (provider) do nothing",
                            provider,
                            entry.last_failure or "quarantined",
                            self._instance,
                        )
                await self._apply_quarantines(conn)
        except Exception:
            logger.warning("health flush failed; continuing on the in-memory copy",
                           exc_info=True)

    async def _apply_quarantines(self, conn: asyncpg.Connection) -> None:
        for row in await conn.fetch("select provider, reason from provider_quarantine"):
            self._health.quarantine(row["provider"], row["reason"])
