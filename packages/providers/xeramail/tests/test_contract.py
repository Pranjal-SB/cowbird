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
from cowbird_xeramail import PAGE, SITEKEY, Xeramail

# Recorded, with secrets replaced: every fixture except synthetic_inbox.json.
# Its envelope is the recorded inbox; the message row is synthetic, shaped from
# the fields the site's own JS reads (id, fromAddress, subject, bodyHtml,
# bodyText, receivedAt), because xeramail's test send never arrived.
FIXTURES = Path(__file__).parent / "fixtures"
VALUE = "glowing.room403@xeramail.com"
SECRET = "SECRET-PLACEHOLDER"
ADDRESS = Address(VALUE, "xeramail", state=SECRET)
MID = "7c1e0f4a-2b3d-4e5f-8a9b-0c1d2e3f4a5b"

GENERATE = "POST https://xeramail.com/api/emails/generate"
AVAILABILITY = "POST https://xeramail.com/api/emails/check-availability"
INBOX = "GET https://xeramail.com/api/emails/"
DELETE = "DELETE https://xeramail.com/api/messages/"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    first = load("generate.json")
    second = {"email": {**first["email"], "address": "other2x@xeramail.com"}}
    return {
        # Two answers so the contract's two generates get two addresses.
        GENERATE: Responses(first, second),
        AVAILABILITY: load("local_available.json"),
        INBOX: load("synthetic_inbox.json"),
        DELETE: Reply(200, {"success": True}),
        **overrides,
    }


def xeramail(solver=None, **overrides):
    http = FakeTransport("xeramail", routes(**overrides), solver=solver or FakeSolver())
    return Xeramail(http), http


def sent(http, prefix):
    method, _, url = prefix.partition(" ")
    return [kw for m, u, kw in http.seen if m == method and u.startswith(url)]


class TestXeramailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return xeramail()[0]


async def test_generate_sends_the_turnstile_token_in_the_body():
    provider, http = xeramail()
    await provider.generate()
    assert provider.http.solver.calls == [("turnstile", PAGE, SITEKEY)]
    assert sent(http, GENERATE)[0]["json"] == {
        "domain": "xeramail.com",
        "expirationMinutes": 1440,
        "turnstileToken": "fake-token",
    }


async def test_generate_keeps_the_secret_as_state_and_the_expiry():
    address = await xeramail()[0].generate()
    assert address.value == VALUE
    assert address.state == SECRET
    assert address.expires_at == datetime(2026, 9, 26, 2, 19, 36, tzinfo=UTC)


async def test_a_chosen_local_part_and_domain_are_checked_then_asked_for():
    provider, http = xeramail()
    await provider.generate(GenerateOptions(local="cbtzq91x7", domain="phuturemail.com"))
    assert sent(http, AVAILABILITY)[0]["json"] == {
        "localPart": "cbtzq91x7",
        "domain": "phuturemail.com",
    }
    body = sent(http, GENERATE)[0]["json"]
    assert body["localPart"] == "cbtzq91x7"
    assert body["domain"] == "phuturemail.com"


async def test_a_reserved_local_part_is_refused_before_a_solve():
    provider, _ = xeramail(**{AVAILABILITY: Reply(400, load("local_reserved.json"))})
    with pytest.raises(NotSupported, match="reserved"):
        await provider.generate(GenerateOptions(local="admin"))
    assert provider.http.solver.calls == []


@pytest.mark.parametrize(
    "opts",
    [GenerateOptions(domain="emailfutureme.com"), GenerateOptions(kind=Kind.GMAIL_ALIAS)],
)
async def test_what_the_free_tier_cannot_do_is_refused_before_a_solve(opts):
    provider, _ = xeramail()
    with pytest.raises(NotSupported):
        await provider.generate(opts)
    assert provider.http.solver.calls == []


async def test_without_a_solver_generate_is_not_supported():
    provider = Xeramail(FakeTransport("xeramail", routes()))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.generate()


async def test_a_refused_token_is_the_solvers_fault_not_drift():
    provider, _ = xeramail(**{GENERATE: Reply(403, load("captcha_failed.json"))})
    with pytest.raises(SolverUnavailable):
        await provider.generate()


async def test_solver_unavailable_propagates_before_any_request():
    failure = SolverUnavailable("solver /turnstile: HTTP 500")
    provider, http = xeramail(FakeSolver(token=failure))
    with pytest.raises(SolverUnavailable) as caught:
        await provider.generate()
    assert caught.value is failure
    assert not sent(http, GENERATE)


async def test_list_sends_the_secret_and_costs_no_solve():
    provider, http = xeramail()
    await provider.list(ADDRESS)
    assert provider.http.solver.calls == []
    (call,) = [(u, kw) for m, u, kw in http.seen if m == "GET"]
    assert call[0] == "https://xeramail.com/api/emails/glowing.room403%40xeramail.com"
    assert call[1]["headers"]["X-Email-Secret"] == SECRET


async def test_list_reads_the_rows():
    rows = await xeramail()[0].list(ADDRESS)
    assert [r.id for r in rows] == [MID]
    assert rows[0].sender == "test@xeramail.com"
    assert rows[0].subject == "Your Email Test is Successful!"
    assert rows[0].received_at == datetime(2026, 9, 25, 2, 20, 5, tzinfo=UTC)


async def test_an_empty_inbox_is_an_empty_list():
    provider, _ = xeramail(**{INBOX: load("inbox_empty.json")})
    assert await provider.list(ADDRESS) == []


async def test_get_reads_the_body_from_the_listing_without_a_solve():
    provider, _ = xeramail()
    message = await provider.get(ADDRESS, MID)
    assert message.id == MID
    assert message.html.startswith("<div>")
    assert "working successfully" in message.text
    assert message.links == ("https://xeramail.com/",)
    assert provider.http.solver.calls == []


async def test_get_falls_back_to_the_text_body():
    inbox = load("synthetic_inbox.json")
    del inbox["email"]["messages"][0]["bodyHtml"]
    message = await xeramail(**{INBOX: inbox})[0].get(ADDRESS, MID)
    assert message.text == "Your email address is working successfully."


async def test_a_message_not_in_the_listing_is_gone():
    with pytest.raises(MessageGone):
        await xeramail()[0].get(ADDRESS, "00000000-0000-0000-0000-000000000000")


async def test_a_refused_secret_is_an_expired_address():
    provider, _ = xeramail(**{INBOX: Reply(403, load("bad_secret.json"))})
    with pytest.raises(AddressExpired):
        await provider.list(ADDRESS)


async def test_list_without_state_is_not_supported():
    with pytest.raises(NotSupported):
        await xeramail()[0].list(Address(VALUE, "xeramail"))


async def test_delete_sends_the_secret():
    provider, http = xeramail()
    await provider.delete(ADDRESS, MID)
    (kw,) = sent(http, DELETE)
    assert kw["headers"]["X-Email-Secret"] == SECRET
    assert http.seen[0][1].endswith(f"/api/messages/{MID}")


async def test_deleting_a_missing_message_is_gone():
    provider, _ = xeramail(**{DELETE: Reply(404, load("message_not_found.json"))})
    with pytest.raises(MessageGone):
        await provider.delete(ADDRESS, MID)


@pytest.mark.parametrize(
    "route, answer",
    [
        (GENERATE, {"email": {"address": VALUE}}),
        (GENERATE, Reply(400, {"error": "Captcha token required"})),
        (INBOX, {"x": 1}),
        (INBOX, {"email": {"messages": [{"subject": "no id"}]}}),
        (INBOX, Reply(401, {"error": "Secret is required"})),
        (DELETE, Reply(400, {"error": "?"})),
    ],
)
async def test_a_malformed_answer_is_drift(route, answer):
    provider, _ = xeramail(**{route: answer})
    with pytest.raises(SchemaDrift):
        if route == GENERATE:
            await provider.generate()
        elif route == DELETE:
            await provider.delete(ADDRESS, MID)
        else:
            await provider.list(ADDRESS)
