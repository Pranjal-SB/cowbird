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
from cowbird_smailpro import PAGE, SITEKEY, SmailPro

FIXTURES = Path(__file__).parent / "fixtures"
MID = "1a0d4b40fe403564"
GMAIL = "p.ool.acc.ount.one@gmail.com"
STATE = '{"timestamp":1790274915,"key":"KEY-PLACEHOLDER"}'
ADDRESS = Address(GMAIL, "smailpro", state=STATE)

CREATE = "GET https://smailpro.com/app/create"
INBOX = "POST https://smailpro.com/app/inbox"
MESSAGE = "GET https://smailpro.com/app/message"
GMAIL_INBOX = "GET https://api.sonjj.com/v1/temp_gmail/inbox"
GMAIL_MESSAGE = "GET https://api.sonjj.com/v1/temp_gmail/message"
OUTLOOK_INBOX = "GET https://api.sonjj.com/v1/temp_outlook/inbox"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    first = load("create_gmail.json")
    return {
        # Two answers so the contract's two generates get two addresses.
        CREATE: Responses(first, {**first, "address": "other.alias@gmail.com"}),
        INBOX: load("inbox.json"),
        GMAIL_INBOX: load("sonjj_inbox.json"),
        OUTLOOK_INBOX: load("sonjj_inbox_empty.json"),
        MESSAGE: (FIXTURES / "message_payload.txt").read_text(encoding="utf-8"),
        GMAIL_MESSAGE: load("sonjj_message.json"),
        **overrides,
    }


def smailpro(solver=None, **overrides):
    http = FakeTransport("smailpro", routes(**overrides), solver=solver or FakeSolver())
    return SmailPro(http), http


def headers_of(http, prefix):
    method, _, url = prefix.partition(" ")
    return [kw.get("headers") or {} for m, u, kw in http.seen if m == method and u == url]


class TestSmailProContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return smailpro()[0]


async def test_create_sends_a_turnstile_token_in_x_captcha():
    provider, http = smailpro()
    await provider.generate()
    assert provider.http.solver.calls == [("turnstile", PAGE, SITEKEY)]
    assert headers_of(http, CREATE)[0]["x-captcha"] == "fake-token"


async def test_generate_carries_timestamp_and_key_in_compact_state():
    address = await smailpro()[0].generate()
    assert address.value == GMAIL
    assert address.state == STATE


async def test_each_message_read_solves_its_own_token():
    provider, http = smailpro()
    await provider.get(ADDRESS, MID)
    await provider.get(ADDRESS, MID)
    assert [c[0] for c in provider.http.solver.calls] == ["turnstile", "turnstile"]
    assert all(h["x-captcha"] == "fake-token" for h in headers_of(http, MESSAGE))
    _, _, kw = next(s for s in http.seen if s[1].endswith("/app/message"))
    assert kw["params"] == {"email": GMAIL, "mid": MID}


async def test_the_inbox_call_carries_no_captcha_and_replays_the_state():
    provider, http = smailpro()
    await provider.list(ADDRESS)
    assert provider.http.solver.calls == []
    assert all("x-captcha" not in h for h in headers_of(http, INBOX))
    _, _, kw = next(s for s in http.seen if s[1].endswith("/app/inbox"))
    assert kw["json"] == [{"address": GMAIL, "timestamp": 1790274915, "key": "KEY-PLACEHOLDER"}]


async def test_list_reads_the_rows():
    rows = await smailpro()[0].list(ADDRESS)
    assert [r.id for r in rows] == [MID]
    assert rows[0].sender == "Xeramail Test"
    assert rows[0].subject == "Your Email Test is Successful!"
    assert rows[0].received_at == datetime(2026, 9, 24, 18, 36, 4, tzinfo=UTC)


async def test_get_reads_the_body():
    message = await smailpro()[0].get(ADDRESS, MID)
    assert message.id == MID
    assert message.subject == "Your Email Test is Successful!"
    assert "your email address is working successfully" in message.text
    assert message.html.lstrip().startswith("<div")


async def test_without_a_solver_generate_is_not_supported():
    provider = SmailPro(FakeTransport("smailpro", routes()))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.generate()


async def test_without_a_solver_get_is_not_supported():
    provider = SmailPro(FakeTransport("smailpro", routes()))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.get(ADDRESS, MID)


async def test_solver_unavailable_propagates_unchanged():
    failure = SolverUnavailable("solver /turnstile: HTTP 500")
    provider, http = smailpro(FakeSolver(token=failure))
    with pytest.raises(SolverUnavailable) as caught:
        await provider.generate()
    assert caught.value is failure
    assert not headers_of(http, CREATE)


async def test_an_empty_inbox_is_an_empty_list():
    provider, _ = smailpro(**{GMAIL_INBOX: load("sonjj_inbox_empty.json")})
    assert await provider.list(ADDRESS) == []


async def test_a_message_the_backend_no_longer_has_is_gone():
    provider, _ = smailpro(**{GMAIL_MESSAGE: Reply(400, load("sonjj_message_gone.json"))})
    with pytest.raises(MessageGone):
        await provider.get(ADDRESS, MID)


async def test_a_message_not_in_the_listing_is_gone_without_a_solve():
    provider, _ = smailpro()
    with pytest.raises(MessageGone):
        await provider.get(ADDRESS, "ffffffffffffffff")
    assert provider.http.solver.calls == []


async def test_a_rejected_key_is_an_expired_address():
    provider, _ = smailpro(**{INBOX: load("inbox_rejected.json")})
    with pytest.raises(AddressExpired):
        await provider.list(ADDRESS)


async def test_list_without_state_is_not_supported():
    with pytest.raises(NotSupported):
        await smailpro()[0].list(Address(GMAIL, "smailpro"))


@pytest.mark.parametrize(
    "route, answer",
    [
        (INBOX, {"x": 1}),
        (GMAIL_INBOX, {"x": 1}),
        (GMAIL_INBOX, {"messages": [{"textSubject": "no mid"}]}),
        (GMAIL_MESSAGE, {"x": 1}),
    ],
)
async def test_a_malformed_answer_is_drift(route, answer):
    provider, _ = smailpro(**{route: answer})
    with pytest.raises(SchemaDrift):
        address = await provider.generate() if route == CREATE else ADDRESS
        await provider.get(address, MID)


async def test_the_outlook_kind_asks_for_outlook_and_reads_its_inbox():
    provider, http = smailpro(**{CREATE: load("create_outlook.json")})
    address = await provider.generate(GenerateOptions(kind=Kind.OUTLOOK_ALIAS))
    assert address.value.endswith("@outlook.com")
    _, _, kw = next(s for s in http.seen if s[1].endswith("/app/create"))
    assert kw["params"]["domain"] == "outlook.com"
    assert await provider.list(address) == []


async def test_googlemail_can_be_chosen_by_domain():
    provider, http = smailpro()
    await provider.generate(GenerateOptions(domain="googlemail.com"))
    _, _, kw = next(s for s in http.seen if s[1].endswith("/app/create"))
    assert kw["params"] == {
        "username": "random",
        "type": "alias",
        "domain": "googlemail.com",
        "server": "1",
    }


@pytest.mark.parametrize(
    "opts",
    [
        GenerateOptions(local="me"),
        GenerateOptions(domain="yahoo.com"),
        GenerateOptions(kind=Kind.OUTLOOK_ALIAS, domain="gmail.com"),
        GenerateOptions(kind=Kind.EDU),
    ],
)
async def test_what_the_free_tier_cannot_do_is_refused_before_a_solve(opts):
    provider, _ = smailpro()
    with pytest.raises(NotSupported):
        await provider.generate(opts)
    assert provider.http.solver.calls == []


@pytest.mark.parametrize("route", [CREATE, MESSAGE])
async def test_a_token_smailpro_rejects_is_the_solvers_fault_not_drift(route):
    # A refused Turnstile token says nothing about smailpro's API, so it must
    # not quarantine the provider.
    provider, _ = smailpro(**{route: Reply(403, load("captcha_invalid.json"))})
    with pytest.raises(SolverUnavailable):
        address = await provider.generate() if route == CREATE else ADDRESS
        await provider.get(address, MID)
