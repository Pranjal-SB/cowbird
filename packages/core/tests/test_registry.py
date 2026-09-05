import pytest
from cowbird.errors import NoProviderAvailable
from cowbird.registry import Registry
from cowbird.testing import FakeProvider


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
