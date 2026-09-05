import pytest
from cowbird.errors import (
    MessageLocked,
    NoProviderAvailable,
    NotSupported,
    ProviderDown,
    SchemaDrift,
)
from cowbird.health import HealthStore, Status
from cowbird.models import Address
from cowbird.provider import Provider
from cowbird.registry import HealthTracked, Registry
from cowbird.testing import CAPS, FakeProvider


def test_registered_provider_is_returned_by_name():
    reg = Registry(discover=False)
    reg.register(FakeProvider)
    assert reg.get("fake").name == "fake"


def test_provider_instances_are_cached_so_sessions_are_reused():
    reg = Registry(discover=False)
    reg.register(FakeProvider)
    assert reg.get("fake") is reg.get("fake")


def test_each_provider_gets_its_own_transport():
    made = []

    def factory(name):
        made.append(name)
        return object()

    reg = Registry(transport_factory=factory, discover=False)
    reg.register(FakeProvider)
    reg.get("fake")
    assert made == ["fake"]


def test_unknown_provider_name_raises():
    with pytest.raises(NoProviderAvailable):
        Registry(discover=False).get("nope")


def test_discover_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr("cowbird.registry.entry_points", lambda **kw: calls.append(kw) or [])
    reg = Registry()
    reg.discover()
    reg.discover()
    assert len(calls) == 1


class _FailingListProvider(Provider):
    """Always fails list(), like a backend that died mid-watch."""

    name = "failing"
    caps = CAPS

    async def generate(self, opts=None):
        return Address(value="a@fail.test", provider=self.name)

    async def list(self, address):
        raise ProviderDown("down")

    async def get(self, address, id):
        raise NotImplementedError


async def test_watch_over_default_poll_loop_is_tracked():
    health = HealthStore()
    wrapped = HealthTracked(_FailingListProvider(None), health)
    addr = Address(value="a@fail.test", provider="failing")

    with pytest.raises(ProviderDown):
        async for _ in wrapped.watch(addr, poll=0):
            pass

    assert health.status("failing") == Status.DOWN


async def test_locked_message_does_not_mark_provider_down():
    class LockedGet(FakeProvider):
        async def get(self, address, id):
            raise MessageLocked("locked")

    health = HealthStore()
    wrapped = HealthTracked(LockedGet(None), health)
    addr = Address(value="a@fake.test", provider="fake")

    with pytest.raises(MessageLocked):
        await wrapped.get(addr, "1")

    assert health.status("fake") == Status.OK


async def test_not_supported_delete_does_not_mark_provider_down():
    health = HealthStore()
    wrapped = HealthTracked(FakeProvider(None), health)
    addr = Address(value="a@fake.test", provider="fake")

    with pytest.raises(NotSupported):
        await wrapped.delete(addr, "1")

    assert health.status("fake") == Status.OK


async def test_schema_drift_still_quarantines():
    class DriftGet(FakeProvider):
        async def get(self, address, id):
            raise SchemaDrift("fake", expected="json", got="html")

    health = HealthStore()
    wrapped = HealthTracked(DriftGet(None), health)
    addr = Address(value="a@fake.test", provider="fake")

    with pytest.raises(SchemaDrift):
        await wrapped.get(addr, "1")

    assert health.status("fake") == Status.QUARANTINED


def test_discover_skips_broken_entry_point_but_registers_others(monkeypatch):
    class FakeEntryPoint:
        def __init__(self, name, loader):
            self.name = name
            self._loader = loader

        def load(self):
            return self._loader()

    def _broken():
        raise RuntimeError("boom")

    eps = [FakeEntryPoint("bad", _broken), FakeEntryPoint("fake", lambda: FakeProvider)]
    monkeypatch.setattr("cowbird.registry.entry_points", lambda **kw: eps)

    reg = Registry()
    reg.discover()

    assert reg.get("fake").name == "fake"
    assert "bad" in reg._failed

    # A retry is not a silent no-op, and does not re-raise or duplicate.
    reg.discover()
    assert reg.get("fake").name == "fake"
