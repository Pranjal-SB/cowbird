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
from cowbird_cftempmail import CfTempMail

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://temp-email-api.awsl.uk"
SITEKEY = "0x4AAAAAAAZSO_9yWmz_-itl"
JWT = "JWT-PLACEHOLDER"
VALUE = "tmpwdr154ebyfg@awsl.uk"
ADDRESS = Address(VALUE, "cftempmail", state=JWT)
MID = "3395874"

SETTINGS = f"GET {API}/open_api/settings"
NEW = f"POST {API}/api/new_address"
MAILS = f"GET {API}/api/mails"
MAIL = f"GET {API}/api/mail/"
DELETE = f"DELETE {API}/api/mails/"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    first = load("new_address.json")
    return {
        SETTINGS: load("settings.json"),
        # Two answers so the contract's two generates get two addresses.
        NEW: Responses(first, {**first, "address": "tmpother@awsl.uk"}),
        MAILS: load("mails.json"),
        MAIL: load("mail.json"),
        DELETE: load("delete.json"),
        **overrides,
    }


def cftempmail(solver=None, **overrides):
    http = FakeTransport("cftempmail", routes(**overrides), solver=solver or FakeSolver())
    return CfTempMail(http), http


def calls(http, prefix):
    method, _, url = prefix.partition(" ")
    return [kw for m, u, kw in http.seen if m == method and u.startswith(url)]


class TestCfTempMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return cftempmail()[0]


async def test_the_turnstile_token_goes_into_new_address():
    provider, http = cftempmail()
    await provider.generate()
    assert provider.http.solver.calls == [("turnstile", "https://mail.awsl.uk/", SITEKEY)]
    body = calls(http, NEW)[0]["json"]
    assert body["cf_token"] == "fake-token"
    assert body["domain"] in ("awsl.uk", "kksk.uk", "misaka.boo")


async def test_generate_keeps_the_jwt_as_state():
    address = await cftempmail()[0].generate()
    assert address.value == VALUE
    assert address.state == JWT


async def test_a_chosen_domain_is_asked_for():
    provider, http = cftempmail()
    await provider.generate(GenerateOptions(domain="misaka.boo"))
    assert calls(http, NEW)[0]["json"]["domain"] == "misaka.boo"


async def test_without_a_solver_generate_is_not_supported():
    http = FakeTransport("cftempmail", routes())
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await CfTempMail(http).generate()
    assert not calls(http, NEW)


async def test_an_instance_without_a_sitekey_needs_no_solve():
    settings = {**load("settings.json"), "cfTurnstileSiteKey": ""}
    http = FakeTransport("cftempmail", routes(**{SETTINGS: settings}))
    await CfTempMail(http).generate()
    assert "cf_token" not in calls(http, NEW)[0]["json"]


async def test_a_refused_token_is_the_solvers_fault_not_drift():
    provider, _ = cftempmail(**{NEW: Reply(400, text("turnstile_refused.txt"))})
    with pytest.raises(SolverUnavailable):
        await provider.generate()


async def test_solver_unavailable_propagates_unchanged():
    failure = SolverUnavailable("solver /turnstile: HTTP 500")
    provider, http = cftempmail(FakeSolver(token=failure))
    with pytest.raises(SolverUnavailable) as caught:
        await provider.generate()
    assert caught.value is failure
    assert not calls(http, NEW)


@pytest.mark.parametrize(
    "opts",
    [
        GenerateOptions(domain="gmail.com"),
        GenerateOptions(local="me"),
        GenerateOptions(kind=Kind.GMAIL_ALIAS),
    ],
)
async def test_what_the_instance_cannot_do_is_refused_before_a_solve(opts):
    provider, http = cftempmail()
    with pytest.raises(NotSupported):
        await provider.generate(opts)
    assert provider.http.solver.calls == []
    assert not calls(http, NEW)


@pytest.mark.parametrize(
    "flag, value",
    [
        ("needAuth", True),
        ("enableUserCreateEmail", False),
        ("disableAnonymousUserCreateEmail", True),
    ],
)
async def test_an_instance_closed_to_anonymous_creation_is_not_supported(flag, value):
    settings = {**load("settings.json"), flag: value}
    provider, _ = cftempmail(**{SETTINGS: settings})
    with pytest.raises(NotSupported):
        await provider.generate()
    assert provider.http.solver.calls == []


async def test_list_sends_the_bearer_jwt_and_no_solve():
    provider, http = cftempmail()
    await provider.list(ADDRESS)
    assert provider.http.solver.calls == []
    kw = calls(http, MAILS)[0]
    assert kw["headers"]["Authorization"] == f"Bearer {JWT}"
    assert kw["params"] == {"limit": 100, "offset": 0}


async def test_list_reads_the_rows():
    rows = await cftempmail()[0].list(ADDRESS)
    assert [r.id for r in rows] == [MID]
    assert rows[0].subject == "Your Email Test is Successful!"
    assert rows[0].sender == "Xeramail Test <test@xeramail.com>"
    assert rows[0].received_at == datetime(2026, 9, 25, 2, 19, 59, tzinfo=UTC)


async def test_an_empty_inbox_is_an_empty_list():
    provider, _ = cftempmail(**{MAILS: load("mails_empty.json")})
    assert await provider.list(ADDRESS) == []


async def test_a_refused_jwt_is_an_expired_address():
    provider, _ = cftempmail(**{MAILS: Reply(401, text("invalid_credential.txt"))})
    with pytest.raises(AddressExpired):
        await provider.list(ADDRESS)


async def test_list_without_state_is_not_supported():
    with pytest.raises(NotSupported):
        await cftempmail()[0].list(Address(VALUE, "cftempmail"))


async def test_get_parses_the_raw_message():
    provider, http = cftempmail()
    message = await provider.get(ADDRESS, MID)
    assert message.id == MID
    assert message.subject == "Your Email Test is Successful!"
    assert message.text.startswith("Congratulations! Your email address is working.")
    assert "working successfully" in message.html
    assert message.received_at == datetime(2026, 9, 25, 2, 19, 59, tzinfo=UTC)
    assert calls(http, MAIL)[0]["headers"]["Authorization"] == f"Bearer {JWT}"
    assert provider.http.solver.calls == []


async def test_a_missing_message_is_gone():
    # The worker answers `null` with HTTP 200 for an id it does not have.
    provider, _ = cftempmail(**{MAIL: text("mail_gone.json")})
    with pytest.raises(MessageGone):
        await provider.get(ADDRESS, "999999999")


async def test_delete_sends_the_bearer_jwt():
    provider, http = cftempmail()
    await provider.delete(ADDRESS, MID)
    kw = calls(http, DELETE)[0]
    assert kw["headers"]["Authorization"] == f"Bearer {JWT}"


async def test_an_instance_that_forbids_deletion_is_not_supported():
    provider, _ = cftempmail(**{DELETE: Reply(403, "User delete email is disabled")})
    with pytest.raises(NotSupported):
        await provider.delete(ADDRESS, MID)


@pytest.mark.parametrize(
    "route, answer",
    [
        (SETTINGS, {"x": 1}),
        (SETTINGS, {**{"domains": []}, "cfTurnstileSiteKey": SITEKEY}),
        (NEW, {"x": 1}),
        (NEW, Reply(400, "Failed to create address")),
        (MAILS, {"x": 1}),
        (MAILS, {"results": [{"raw": "no id"}], "count": 1}),
        (MAIL, {"x": 1}),
    ],
)
async def test_a_malformed_answer_is_drift(route, answer):
    provider, _ = cftempmail(**{route: answer})
    with pytest.raises(SchemaDrift):
        if route in (SETTINGS, NEW):
            await provider.generate()
        else:
            await provider.get(ADDRESS, MID) if route == MAIL else await provider.list(ADDRESS)
