import json
from dataclasses import replace

import pytest
from cowbird.models import Message, MessageRow
from cowbird.testing import CAPS, FakeProvider


async def _noop() -> None:
    return None


class CodeProvider(FakeProvider):
    """Like FakeProvider, but get() returns a body carrying an OTP so the
    otp()/watch() path has something real to extract."""

    name = "fakeotp"

    async def get(self, address, id):
        return Message(
            id=id,
            sender="s@x.test",
            subject="your code",
            received_at=None,
            html="",
            text="use 294819 to sign in",
        )


class NeedsStateProvider(FakeProvider):
    """caps.needs_state=True, mirroring mail.tm."""

    name = "fakestate"
    caps = replace(CAPS, needs_state=True)


class DeleteOnlyProvider(FakeProvider):
    """caps.delete=True but caps.needs_state=False: proves delete is no
    longer used as a needs-state proxy."""

    name = "fakedel"
    caps = replace(CAPS, delete=True)


def _registry(*classes):
    from cowbird.registry import Registry

    reg = Registry(transport_factory=lambda name: None, discover=False)
    for cls in classes:
        reg.register(cls)
    return reg


@pytest.fixture
def fake_pool(monkeypatch):
    from cowbird.health import HealthStore
    from cowbird.pool import Pool

    pool = Pool(_registry(FakeProvider), HealthStore())
    monkeypatch.setattr("cowbird_cli.default_pool", lambda: pool)
    monkeypatch.setattr("cowbird_cli.aclose_default_pool", _noop)
    return pool


@pytest.fixture
def multi_pool(monkeypatch):
    """Several providers registered so --provider pinning and caps-based
    behaviour are actually exercised, not trivially satisfied by the only
    provider in the registry."""
    from cowbird.health import HealthStore
    from cowbird.pool import Pool

    pool = Pool(
        _registry(FakeProvider, CodeProvider, NeedsStateProvider, DeleteOnlyProvider),
        HealthStore(),
    )
    monkeypatch.setattr("cowbird_cli.default_pool", lambda: pool)
    monkeypatch.setattr("cowbird_cli.aclose_default_pool", _noop)
    return pool


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


def test_new_provider_pin_is_honoured(multi_pool, capsys):
    from cowbird_cli import main

    assert main(["new", "--provider", "fakeotp"]) == 0
    out = capsys.readouterr().out
    assert "fakeotp" in out


def test_new_gmail_with_no_matching_provider_fails_cleanly(multi_pool, capsys):
    from cowbird_cli import main

    assert main(["new", "--gmail"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err


def test_providers_lists_the_health_matrix(fake_pool, capsys):
    from cowbird_cli import main

    assert main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "PROVIDER" in out and "fake" in out and "ok" in out


def test_unknown_command_exits_nonzero(capsys):
    from cowbird_cli import main

    with pytest.raises(SystemExit):
        main(["frobnicate"])


def test_wait_otp_prints_only_the_bare_code(multi_pool, capsys):
    from cowbird_cli import main

    multi_pool.registry.get("fakeotp").pages = [
        [MessageRow(id="1", sender="s@x.test", subject="c", received_at=None)]
    ]
    assert main(["wait", "a@fake.test", "--provider", "fakeotp", "--otp"]) == 0
    assert capsys.readouterr().out == "294819\n"


def test_wait_without_otp_prints_sender_and_subject(fake_pool, capsys):
    from cowbird_cli import main

    fake_pool.registry.get("fake").pages = [
        [MessageRow(id="1", sender="s@x.test", subject="hi", received_at=None)]
    ]
    assert main(["wait", "a@fake.test", "--provider", "fake"]) == 0
    out = capsys.readouterr().out
    assert "s@x.test" in out and "hi" in out


def test_wait_timeout_exits_2_and_writes_to_stderr_not_stdout(fake_pool, capsys):
    from cowbird_cli import main

    code = main(
        ["wait", "a@fake.test", "--provider", "fake", "--otp", "--timeout", "0.05"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "timeout" in captured.err


def test_wait_cowbird_error_exits_1_and_writes_to_stderr(fake_pool, capsys):
    from cowbird_cli import main

    code = main(["wait", "a@fake.test", "--provider", "missing"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "error:" in captured.err


def test_wait_without_state_fails_fast_when_provider_needs_it(multi_pool, capsys):
    from cowbird_cli import main

    code = main(["wait", "a@fake.test", "--provider", "fakestate"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "--state" in captured.err
    assert "new --json" in captured.err


def test_wait_without_state_is_fine_when_provider_does_not_need_it(multi_pool, capsys):
    # fakedel has caps.delete=True but caps.needs_state=False: the old
    # delete-based proxy would have wrongly blocked this.
    from cowbird_cli import main

    multi_pool.registry.get("fakedel").pages = [
        [MessageRow(id="1", sender="s@x.test", subject="hi", received_at=None)]
    ]
    code = main(["wait", "a@fake.test", "--provider", "fakedel"])
    assert code == 0
    assert capsys.readouterr().err == ""
