import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_reusableemail import ReusableEmail

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "1"
ADDRESS = Address("cb4e5581de14@reusable.email", "reusableemail")
ENSURE = "POST https://api.reusable.email/v1/inbox/ensure?"
TOKEN = "/token?"
LIST = "?limit=20"
EMAIL = f"/emails/{MESSAGE_ID}?"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        ENSURE: Reply(201, load("ensure.json")),
        TOKEN: load("token.json"),
        LIST: load("inbox.json"),
        EMAIL: load("email.json"),
        **overrides,
    }


def calls(http, needle):
    return [(m, u, kw) for m, u, kw in http.seen if needle in f"{m} {u}"]


class TestReusableEmailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return ReusableEmail(FakeTransport("reusableemail", routes()))


async def test_generate_ensures_the_inbox():
    http = FakeTransport("reusableemail", routes())
    address = await ReusableEmail(http).generate()
    [(_, _, kw)] = calls(http, "/v1/inbox/ensure")
    assert kw["json"] == {"address": address.value}
    assert address.value.startswith("cb") and address.value.endswith("@reusable.email")


async def test_generate_honours_a_chosen_local():
    address = await ReusableEmail(FakeTransport("reusableemail", routes())).generate(
        GenerateOptions(local="hello", domain="reusable.email")
    )
    assert address.value == "hello@reusable.email"


async def test_a_domain_it_does_not_serve_is_refused():
    with pytest.raises(NotSupported):
        await ReusableEmail(FakeTransport("reusableemail", routes())).generate(
            GenerateOptions(domain="gmail.com")
        )


async def test_list_sends_the_inbox_token():
    http = FakeTransport("reusableemail", routes())
    await ReusableEmail(http).list(ADDRESS)
    [(_, url, kw)] = calls(http, "GET ")
    assert url == "https://api.reusable.email/v1/inbox/cb4e5581de14%40reusable.email"
    assert kw["headers"]["Authorization"] == f"Inbox {load('token.json')['token']}"


async def test_list_parses_the_rows():
    rows = await ReusableEmail(FakeTransport("reusableemail", routes())).list(ADDRESS)
    assert [r.id for r in rows] == ["1"]
    assert rows[0].sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert rows[0].subject == "JoltMx test email (ref 01a0d377706d)"
    assert rows[0].received_at == datetime(2026, 9, 24, 12, 50, 15, tzinfo=UTC)


async def test_the_token_is_minted_once_per_address():
    http = FakeTransport("reusableemail", routes())
    provider = ReusableEmail(http)
    await provider.list(ADDRESS)
    await provider.get(ADDRESS, MESSAGE_ID)
    assert len(calls(http, "/token")) == 1


async def test_an_empty_inbox_is_an_empty_list():
    http = FakeTransport("reusableemail", routes(**{LIST: load("inbox_empty.json")}))
    assert await ReusableEmail(http).list(ADDRESS) == []


async def test_get_parses_sender_subject_utc_date_and_body():
    message = await ReusableEmail(FakeTransport("reusableemail", routes())).get(ADDRESS, MESSAGE_ID)
    assert message.id == MESSAGE_ID
    assert message.sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert message.subject == "JoltMx test email (ref 01a0d377706d)"
    assert message.received_at == datetime(2026, 9, 24, 12, 50, 15, tzinfo=UTC)
    assert "everything is working as expected" in message.text
    assert message.html
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)


async def test_an_unknown_id_is_gone():
    http = FakeTransport("reusableemail", routes(**{EMAIL: Reply(404, load("email_missing.json"))}))
    with pytest.raises(MessageGone):
        await ReusableEmail(http).get(ADDRESS, MESSAGE_ID)


async def test_a_list_without_emails_is_drift():
    http = FakeTransport("reusableemail", routes(**{LIST: {"inbox": {}, "rows": []}}))
    with pytest.raises(SchemaDrift):
        await ReusableEmail(http).list(ADDRESS)


async def test_a_rejected_token_is_reminted_once():
    unauthorized = Reply(401, load("unauthorized.json"))
    http = FakeTransport(
        "reusableemail", routes(**{LIST: Responses(unauthorized, load("inbox.json"))})
    )
    rows = await ReusableEmail(http).list(ADDRESS)
    assert [r.id for r in rows] == ["1"]
    assert len(calls(http, "/token")) == 2


async def test_a_token_rejected_twice_is_not_retried_forever():
    http = FakeTransport("reusableemail", routes(**{LIST: Reply(401, load("unauthorized.json"))}))
    with pytest.raises(ProviderDown):
        await ReusableEmail(http).list(ADDRESS)
    assert len(calls(http, "/token")) == 2


async def test_an_inbox_that_does_not_exist_yet_is_ensured_before_the_token():
    http = FakeTransport(
        "reusableemail",
        routes(**{TOKEN: Responses(Reply(404, load("token_missing.json")), load("token.json"))}),
    )
    await ReusableEmail(http).list(Address("cbnew@reusable.email", "reusableemail"))
    assert len(calls(http, "/v1/inbox/ensure")) == 1
    assert len(calls(http, "/token")) == 2
