import os
from datetime import UTC, datetime, timedelta

import pytest
from cowbird.models import Address
from cowbird_server import db
from cowbird_server.pgstore import PostgresStore
from cowbird_server.store import Store, StoreUnavailable

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="no DATABASE_URL"),
]


@pytest.fixture
async def store():
    pool = await db.connect(os.environ["DATABASE_URL"])
    await db.migrate(pool)
    async with pool.acquire() as conn:
        await conn.execute("truncate addresses")
    yield PostgresStore(pool)
    await pool.close()


def address(value="a@fake.test", expires_at=None) -> Address:
    return Address(value=value, provider="fake", expires_at=expires_at, state="secret-jwt")


async def test_postgresstore_satisfies_the_protocol(store):
    assert isinstance(store, Store)


async def test_put_is_an_upsert_rather_than_a_conflict(store):
    # Two instances can legitimately write the same address: one issues it, the
    # other refreshes provider state on it. A duplicate-key error here would
    # surface as a 500 on a perfectly ordinary request.
    await store.put(address())
    await store.put(address())
    assert (await store.get("a@fake.test")).state == "secret-jwt"


async def test_an_expired_row_is_evicted_on_read(store):
    await store.put(address(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert await store.get("a@fake.test") is None
    async with store._pool.acquire() as conn:
        assert await conn.fetchval("select count(*) from addresses") == 0


async def test_sweep_deletes_expired_rows_nobody_asked_for(store):
    # The leak MemoryStore has: a row nobody reads is never evicted.
    await store.put(address("old@fake.test", datetime.now(UTC) - timedelta(seconds=1)))
    await store.put(address("live@fake.test"))
    assert await store.sweep() == 1
    assert await store.get("live@fake.test") is not None


async def test_a_dead_pool_raises_storeunavailable_not_a_driver_error(store):
    # The route layer maps StoreUnavailable to 503. A raw asyncpg error is not a
    # CowbirdError, so it would fall through to the catch-all and become a 500,
    # telling a client not to retry something that is only transiently broken.
    await store._pool.close()
    with pytest.raises(StoreUnavailable):
        await store.get("a@fake.test")


async def test_aclose_on_an_already_closed_pool_does_not_raise(store):
    # Unlike put/get/sweep, aclose runs at shutdown with no request to answer
    # and nothing for a client to retry, so it swallows driver errors instead
    # of raising StoreUnavailable.
    await store._pool.close()
    await store.aclose()
