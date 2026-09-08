import os
from datetime import UTC, datetime, timedelta

import pytest
from cowbird.models import Address
from cowbird_server import db
from cowbird_server.pgstore import PostgresStore
from cowbird_server.store import MemoryStore, Store

_HAS_PG = bool(os.environ.get("DATABASE_URL"))


def address(value="a@fake.test", expires_at=None) -> Address:
    return Address(value=value, provider="fake", expires_at=expires_at, state="secret-jwt")


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.pg,
                pytest.mark.skipif(not _HAS_PG, reason="no DATABASE_URL"),
            ],
        ),
    ]
)
async def store(request):
    """Both implementations, one contract.

    Two hand-written suites for two implementations of the same protocol drift,
    and the one that drifts is always the one without a running database.
    """
    if request.param == "memory":
        yield MemoryStore()
        return
    pool = await db.connect(os.environ["DATABASE_URL"])
    await db.migrate(pool)
    async with pool.acquire() as conn:
        await conn.execute("truncate addresses")
    yield PostgresStore(pool)
    await pool.close()


async def test_an_address_survives_a_round_trip_with_its_state(store):
    # The state is the entire point. mail.tm binds an inbox to a JWT issued at
    # generate time; drop it and the address cannot be read back at all.
    await store.put(address())
    assert (await store.get("a@fake.test")).state == "secret-jwt"


async def test_an_unknown_address_is_none_rather_than_an_error(store):
    assert await store.get("nobody@fake.test") is None


async def test_an_expired_address_is_gone(store):
    await store.put(address(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert await store.get("a@fake.test") is None


def test_both_implementations_satisfy_the_protocol():
    # issubclass works here because Store declares only methods; a protocol with
    # data members would reject it. PostgresStore is checked as a class rather
    # than an instance because constructing one needs a live pool.
    assert isinstance(MemoryStore(), Store)
    assert issubclass(PostgresStore, Store)


async def test_memorystore_evicts_rather_than_hiding():
    # size() is MemoryStore's own diagnostic and not part of the protocol, so
    # this one stays specific to it.
    store = MemoryStore()
    await store.put(address(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert await store.get("a@fake.test") is None
    assert store.size() == 0


async def test_aclose_on_memorystore_does_not_drop_rows():
    store = MemoryStore()
    await store.put(address())
    await store.aclose()
    assert (await store.get("a@fake.test")).state == "secret-jwt"
