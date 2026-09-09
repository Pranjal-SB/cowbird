"""Connection pool and schema migration.

asyncpg directly, no ORM. There are three tables plus a version row, the
queries are short, and the dataclasses describing this data already live in
core; an ORM would mean maintaining a second description of the same tables.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger("cowbird.server")

_MIGRATIONS = Path(__file__).parent / "migrations"
# Any constant works as long as it never changes: two instances must pick the
# same number or the lock does not serialise them against each other.
_LOCK_ID = 0x00C0B14D


# Both exist for the same failure: a TCP connection that is established and
# then blackholed -- a NAT idle-drop, a failover that drops packets instead of
# resetting them. Without them an in-flight query never returns and never gives
# its pool holder back, ten of those exhaust the pool, and every later request
# blocks in acquire() forever with nothing raised and nothing logged while
# /health still answers 200.
#
# COMMAND_TIMEOUT is generous because migrate() waits on an advisory lock while
# another instance applies the same files; every other statement here touches
# one row or one small table and finishes in milliseconds.
COMMAND_TIMEOUT = 15.0
# ACQUIRE_TIMEOUT is short because waiting on the pool means all ten holders are
# busy, and a caller that has already waited five seconds for a slot is better
# off as a 503 the client can retry than as a request nobody answers.
ACQUIRE_TIMEOUT = 5.0


async def connect(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn, min_size=1, max_size=10, command_timeout=COMMAND_TIMEOUT
    )


def _files() -> list[tuple[int, Path]]:
    """Migrations as (version, path), ordered. A name that does not start with
    digits is a mistake, not a migration, and is skipped loudly."""
    found = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        match = re.match(r"^(\d+)_", path.name)
        if match is None:
            logger.warning("ignoring unnumbered migration %s", path.name)
            continue
        found.append((int(match.group(1)), path))
    return sorted(found)


async def migrate(pool: asyncpg.Pool) -> None:
    """Apply every migration newer than the recorded version.

    The advisory lock is held for the whole run, so two instances starting
    together serialise instead of both applying the same file. Compose starts
    both servers at once, which makes that the normal case rather than a race
    worth ignoring.
    """
    async with pool.acquire(timeout=ACQUIRE_TIMEOUT) as conn:
        # The lock must come first: two connections both racing "create table
        # if not exists" can pass the existence check together and collide on
        # the catalog insert, which is exactly the case this lock exists for.
        await conn.execute("select pg_advisory_lock($1)", _LOCK_ID)
        try:
            await conn.execute(
                "create table if not exists schema_version ("
                " version int primary key, applied_at timestamptz not null default now())"
            )
            current = await conn.fetchval("select coalesce(max(version), 0) from schema_version")
            for version, path in _files():
                if version <= current:
                    continue
                # One transaction per migration: a failure half way leaves the
                # earlier ones applied and recorded, which is recoverable, and
                # the failed one wholly absent, which is the point.
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute(
                        "insert into schema_version (version) values ($1)", version
                    )
                logger.info("applied migration %s", path.name)
        finally:
            await conn.execute("select pg_advisory_unlock($1)", _LOCK_ID)
