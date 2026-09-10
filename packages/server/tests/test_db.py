import os

import asyncpg
import pytest
from cowbird_server import db

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="no DATABASE_URL"),
]


@pytest.fixture
async def pool(pg_pool):
    p = await pg_pool()
    # Every pg test starts from an empty schema so ordering between tests
    # cannot decide whether one passes.
    async with p.acquire() as conn:
        await conn.execute(
            "drop table if exists addresses, provider_health, "
            "provider_quarantine, schema_version"
        )
    yield p
    await p.close()


async def test_migrate_creates_the_tables(pool, pg_schema):
    await db.migrate(pool)
    async with pool.acquire() as conn:
        names = {
            r["tablename"]
            for r in await conn.fetch(
                "select tablename from pg_tables where schemaname = $1", pg_schema
            )
        }
    assert {"addresses", "provider_health", "provider_quarantine"} <= names


async def test_migrate_is_idempotent(pool):
    # Every instance runs this at startup, on every restart, forever.
    await db.migrate(pool)
    await db.migrate(pool)
    async with pool.acquire() as conn:
        assert await conn.fetchval("select max(version) from schema_version") == 1


async def test_two_instances_migrating_at_once_do_not_collide(pool, pg_pool):
    # Compose starts both servers together, so this race happens on the very
    # first run rather than being theoretical.
    import asyncio

    other = await pg_pool()
    try:
        await asyncio.gather(db.migrate(pool), db.migrate(other))
    finally:
        await other.close()
    async with pool.acquire() as conn:
        assert await conn.fetchval("select count(*) from schema_version") == 1


async def test_connect_raises_on_a_bad_dsn():
    with pytest.raises((OSError, asyncpg.PostgresError)):
        await db.connect("postgresql://nobody@127.0.0.1:1/none")
