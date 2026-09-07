from datetime import UTC, datetime, timedelta

from cowbird.models import Address
from cowbird_server.store import MemoryStore, Store


def address(value="a@fake.test", expires_at=None) -> Address:
    return Address(value=value, provider="fake", expires_at=expires_at, state="secret-jwt")


async def test_an_address_survives_a_round_trip_with_its_state():
    # The state is the entire point. mail.tm binds an inbox to a JWT issued at
    # generate time; drop it and the address cannot be read back at all.
    store = MemoryStore()
    await store.put(address())
    assert (await store.get("a@fake.test")).state == "secret-jwt"


async def test_an_unknown_address_is_none_rather_than_an_error():
    assert await MemoryStore().get("nobody@fake.test") is None


async def test_an_expired_address_is_gone_and_is_evicted():
    store = MemoryStore()
    await store.put(address(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert await store.get("a@fake.test") is None
    # Evicted, not merely hidden: a store that answers None forever while
    # holding the row is a memory leak with a polite face.
    assert store.size() == 0


async def test_delete_is_idempotent():
    store = MemoryStore()
    await store.delete("never-existed@fake.test")
    await store.put(address())
    await store.delete("a@fake.test")
    assert await store.get("a@fake.test") is None


def test_memorystore_satisfies_the_protocol():
    # PostgresStore implements this same protocol. Pinning it here
    # makes the seam a contract rather than a convention.
    assert isinstance(MemoryStore(), Store)
