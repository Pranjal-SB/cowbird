import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import (
    AddressExpired,
    MessageGone,
    NotSupported,
    SchemaDrift,
    SolverUnavailable,
)
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeSolver, FakeTransport, Reply, Responses
from cowbird_etempmail import PAGE, SITEKEY, ETempMail

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "hivxwx3mdh@temporarmail.edu.pl"
SESSION = "SESSION-PLACEHOLDER"
ADDRESS = Address(EMAIL, "etempmail", state=json.dumps({"ci_session": SESSION}))

CREATE = "POST https://etempmail.com/getEmailAddress"
INBOX = "POST https://etempmail.com/getInbox"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def created(**fields):
    return Reply(200, {**load("create.json"), **fields}, cookies={"ci_session": SESSION})


def routes(**overrides):
    return {
        # Two answers so the contract's two generates get two addresses.
        CREATE: Responses(created(), created(address="other1234@mats.edu.pl")),
        INBOX: json.dumps(load("inbox.json")),
        **overrides,
    }


def etempmail(solver=None, **overrides):
    http = FakeTransport("etempmail", routes(**overrides), solver=solver or FakeSolver())
    return ETempMail(http), http


def sent(http, prefix):
    method, _, url = prefix.partition(" ")
    return [kw for m, u, kw in http.seen if m == method and u == url]


class TestETempMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return etempmail()[0]


def test_caps_need_a_solver_and_a_fresh_session_per_request():
    caps = ETempMail.caps
    assert caps.needs_solver and caps.fresh_session and caps.needs_state
    assert caps.kind == frozenset({Kind.EDU})


async def test_create_posts_one_turnstile_token_as_cf_token():
    provider, http = etempmail()
    await provider.generate()
    assert provider.http.solver.calls == [("turnstile", PAGE, SITEKEY)]
    assert sent(http, CREATE)[0]["data"] == {"cf_token": "fake-token"}


async def test_generate_carries_the_session_cookie_in_state_and_expires_in_20_minutes():
    address = await etempmail()[0].generate()
    assert address.value == EMAIL
    assert json.loads(address.state) == {"ci_session": SESSION}
    assert address.expires_at == datetime.fromtimestamp(1790306080 + 20 * 60, UTC)


async def test_the_inbox_replays_the_cookie_without_a_solve():
    provider, http = etempmail()
    await provider.list(ADDRESS)
    await provider.get(ADDRESS, (await provider.list(ADDRESS))[0].id)
    assert provider.http.solver.calls == []
    assert all(kw["cookies"] == {"ci_session": SESSION} for kw in sent(http, INBOX))


async def test_list_reads_the_rows():
    rows = await etempmail()[0].list(ADDRESS)
    assert len(rows) == 1
    assert rows[0].sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert rows[0].subject == "JoltMx test email (ref 01a0d68f70c2)"
    # The site prints Istanbul time (UTC+3); JoltMx stamped this 03:15:19 UTC.
    assert rows[0].received_at == datetime(2026, 9, 25, 3, 15, 19, tzinfo=UTC)


async def test_ids_are_stable_when_new_mail_shifts_the_listing():
    row = load("inbox.json")[0]
    other = {**row, "subject": "newer", "date": "25/09/2026 06:20:00"}
    first = await etempmail(**{INBOX: json.dumps([row])})[0].list(ADDRESS)
    later = await etempmail(**{INBOX: json.dumps([other, row])})[0].list(ADDRESS)
    assert first[0].id == later[1].id != later[0].id


async def test_get_reads_the_body_from_the_listing():
    provider, _ = etempmail()
    row = (await provider.list(ADDRESS))[0]
    message = await provider.get(ADDRESS, row.id)
    assert message.id == row.id
    assert message.subject == row.subject
    assert message.html.startswith("<!DOCTYPE html>")
    assert "This is a test email from JoltMx" in message.text
    assert "https://joltmx.com" in message.links


async def test_without_a_solver_generate_is_not_supported():
    provider = ETempMail(FakeTransport("etempmail", routes()))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.generate()


async def test_solver_unavailable_propagates_before_any_request():
    failure = SolverUnavailable("solver /turnstile: HTTP 500")
    provider, http = etempmail(FakeSolver(token=failure))
    with pytest.raises(SolverUnavailable) as caught:
        await provider.generate()
    assert caught.value is failure
    assert not sent(http, CREATE)


async def test_a_refused_token_is_the_solvers_fault_not_drift():
    provider, _ = etempmail(**{CREATE: Reply(403, load("bot_detected.json"))})
    with pytest.raises(SolverUnavailable, match="Bot detected"):
        await provider.generate()


async def test_the_decoy_address_means_the_token_was_refused():
    # A call the site does not trust gets a joke address rather than an error.
    provider, _ = etempmail(**{CREATE: Reply(200, load("decoy.json"))})
    with pytest.raises(SolverUnavailable, match="decoy"):
        await provider.generate()


async def test_an_empty_inbox_is_an_empty_list():
    provider, _ = etempmail(**{INBOX: "[]"})
    assert await provider.list(ADDRESS) == []


async def test_a_session_the_site_no_longer_knows_is_an_expired_address():
    provider, _ = etempmail(**{INBOX: ""})
    with pytest.raises(AddressExpired):
        await provider.list(ADDRESS)


async def test_a_message_no_longer_listed_is_gone():
    provider, _ = etempmail(**{INBOX: "[]"})
    with pytest.raises(MessageGone):
        await provider.get(ADDRESS, "0123456789abcdef")


async def test_list_without_state_is_not_supported():
    with pytest.raises(NotSupported):
        await etempmail()[0].list(Address(EMAIL, "etempmail"))


@pytest.mark.parametrize(
    "opts", [GenerateOptions(local="me"), GenerateOptions(domain="mats.edu.pl")]
)
async def test_choices_the_site_cannot_honour_are_refused_before_a_solve(opts):
    provider, _ = etempmail()
    with pytest.raises(NotSupported):
        await provider.generate(opts)
    assert provider.http.solver.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        created(address=None),
        created(creation_time="soon"),
        Reply(200, load("create.json")),  # no session cookie
        Reply(200, "<html>"),
    ],
)
async def test_a_malformed_create_is_drift(answer):
    provider, _ = etempmail(**{CREATE: answer})
    with pytest.raises(SchemaDrift):
        await provider.generate()


@pytest.mark.parametrize("answer", ['{"x": 1}', '[{"subject": "no body"}]', "<html>"])
async def test_a_malformed_inbox_is_drift(answer):
    provider, _ = etempmail(**{INBOX: answer})
    with pytest.raises(SchemaDrift):
        await provider.list(ADDRESS)
