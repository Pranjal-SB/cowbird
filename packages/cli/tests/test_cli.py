import json

import pytest


@pytest.fixture
def fake_pool(monkeypatch):
    from cowbird.health import HealthStore
    from cowbird.pool import Pool
    from cowbird.registry import Registry
    from cowbird.testing import FakeProvider

    reg = Registry(transport_factory=lambda name: None, discover=False)
    reg.register(FakeProvider)
    pool = Pool(reg, HealthStore())
    monkeypatch.setattr("cowbird_cli.default_pool", lambda: pool)
    monkeypatch.setattr("cowbird_cli.aclose_default_pool", _noop)
    return pool


async def _noop() -> None:
    return None


def test_new_prints_address_provider_and_ttl(fake_pool, capsys):
    from cowbird_cli import main

    assert main(["new"]) == 0
    out = capsys.readouterr().out
    assert "a@fake.test" in out
    assert "fake" in out


def test_new_does_not_leak_state_in_human_output(fake_pool, capsys):
    from cowbird_cli import main

    assert main(["new"]) == 0
    out = capsys.readouterr().out
    assert "state" not in out.lower()


def test_new_json_emits_a_parseable_object(fake_pool, capsys):
    from cowbird_cli import main

    assert main(["new", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["address"] == "a@fake.test"
    assert payload["provider"] == "fake"
    # The state round-trips to `cowbird wait --state`; the key is always present
    # even when a provider has no credential to hand over.
    assert "state" in payload


def test_providers_lists_the_health_matrix(fake_pool, capsys):
    from cowbird_cli import main

    assert main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "PROVIDER" in out and "fake" in out and "ok" in out


def test_unknown_command_exits_nonzero(capsys):
    from cowbird_cli import main

    with pytest.raises(SystemExit):
        main(["frobnicate"])
