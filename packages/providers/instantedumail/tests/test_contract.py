import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_instantedumail import APIKEY, InstantEduMail

FIXTURES = Path(__file__).parent / "fixtures"
MAILBOX_ID = "4f2310e8-4ad1-44a0-90c3-7d630a5190a7"
MESSAGE_ID = "4fe6f6fe-3196-40fe-9816-03a976c64647"

SIGNUP = "/auth/v1/signup"
REFRESH = "grant_type=refresh_token"
CREATE = "/rest/v1/mailboxes"
LIST = "folder=eq.inbox"
GET = "&id=eq."


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        SIGNUP: load("signup.json"),
        CREATE: Reply(201, load("mailbox.json")),
        LIST: load("emails_full.json"),
        GET: load("emails_full.json"),
        REFRESH: load("refresh.json"),
        **overrides,
    }


def state():
    return json.dumps(
        {
            "access_token": load("signup.json")["access_token"],
            "refresh_token": load("signup.json")["refresh_token"],
            "mailbox_id": MAILBOX_ID,
        }
    )


def address():
    return Address("campusab12@mail-edu.eu", "instantedumail", state=state())


def calls(http, needle):
    return [(m, u, kw) for m, u, kw in http.seen if needle in f"{m} {u}?{kw.get('params', '')}"]


class TestInstantEduMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return InstantEduMail(FakeTransport("instantedumail", routes()))


async def test_generate_sends_apikey_and_bearer_and_creates_a_random_mailbox():
    http = FakeTransport("instantedumail", routes())
    addr = await InstantEduMail(http).generate()
    assert addr.value.endswith("@mail-edu.eu")
    assert addr.provider == "instantedumail"
    [(_, _, sk)] = calls(http, "/auth/v1/signup")
    assert sk["headers"]["apikey"] == APIKEY
    [(_, _, ck)] = calls(http, "/rest/v1/mailboxes")
    assert ck["headers"]["apikey"] == APIKEY
    assert ck["headers"]["Authorization"] == f"Bearer {load('signup.json')['access_token']}"
    assert ck["json"]["mailbox_type"] == "random"
    assert ck["json"]["address"] == addr.value
    assert "expires_at" in ck["json"]


async def test_generate_stores_tokens_and_mailbox_id_in_state():
    addr = await InstantEduMail(FakeTransport("instantedumail", routes())).generate()
    st = json.loads(addr.state)
    assert st["access_token"] == load("signup.json")["access_token"]
    assert st["refresh_token"] == load("signup.json")["refresh_token"]
    assert st["mailbox_id"] == MAILBOX_ID


async def test_generate_honours_a_chosen_local():
    addr = await InstantEduMail(FakeTransport("instantedumail", routes())).generate(
        GenerateOptions(local="scholar99", domain="chicago.io.vn")
    )
    assert addr.value == "scholar99@chicago.io.vn"


async def test_a_domain_it_does_not_serve_is_refused():
    with pytest.raises(NotSupported):
        await InstantEduMail(FakeTransport("instantedumail", routes())).generate(
            GenerateOptions(domain="gmail.com")
        )


async def test_list_sends_apikey_and_bearer():
    http = FakeTransport("instantedumail", routes())
    await InstantEduMail(http).list(address())
    [(_, _, kw)] = calls(http, "/rest/v1/emails")
    assert kw["headers"]["apikey"] == APIKEY
    assert kw["headers"]["Authorization"] == f"Bearer {load('signup.json')['access_token']}"


async def test_list_parses_rows():
    rows = await InstantEduMail(FakeTransport("instantedumail", routes())).list(address())
    assert [r.id for r in rows] == [MESSAGE_ID]
    assert rows[0].sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert rows[0].subject == "JoltMx test email (ref 01a0d7b1130c)"
    assert rows[0].received_at == datetime(2026, 9, 25, 8, 31, 42, 671000, tzinfo=UTC)


async def test_an_empty_inbox_is_an_empty_list():
    http = FakeTransport("instantedumail", routes(**{LIST: load("emails_empty.json")}))
    assert await InstantEduMail(http).list(address()) == []


async def test_get_parses_body_and_links():
    message = await InstantEduMail(FakeTransport("instantedumail", routes())).get(
        address(), MESSAGE_ID
    )
    assert message.id == MESSAGE_ID
    assert message.sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert "everything is working as expected" in message.text
    assert message.html
    assert any("joltmx.com" in href for href in message.links)


async def test_an_unknown_id_is_gone():
    http = FakeTransport("instantedumail", routes(**{GET: load("emails_empty.json")}))
    with pytest.raises(MessageGone):
        await InstantEduMail(http).get(address(), MESSAGE_ID)


async def test_a_malformed_uuid_is_gone():
    http = FakeTransport("instantedumail", routes(**{GET: Reply(400, load("bad_id.json"))}))
    with pytest.raises(MessageGone):
        await InstantEduMail(http).get(address(), "notauuid")


async def test_a_list_that_is_not_an_array_is_drift():
    http = FakeTransport("instantedumail", routes(**{LIST: load("drift.json")}))
    with pytest.raises(SchemaDrift):
        await InstantEduMail(http).list(address())


async def test_an_expired_token_is_refreshed_once():
    expired = Reply(401, load("jwt_expired.json"))
    http = FakeTransport(
        "instantedumail", routes(**{LIST: Responses(expired, load("emails_full.json"))})
    )
    provider = InstantEduMail(http)
    rows = await provider.list(address())
    assert [r.id for r in rows] == [MESSAGE_ID]
    assert len(calls(http, "grant_type=refresh_token")) == 1
    # After refresh, the second read carries the new access token.
    [(_, _, kw)] = calls(http, "grant_type=refresh_token")
    reads = calls(http, "/rest/v1/emails")
    assert reads[-1][2]["headers"]["Authorization"] == (
        f"Bearer {load('refresh.json')['access_token']}"
    )


async def test_a_token_rejected_after_refresh_is_not_retried_forever():
    http = FakeTransport("instantedumail", routes(**{LIST: Reply(401, load("jwt_expired.json"))}))
    with pytest.raises(ProviderDown):
        await InstantEduMail(http).list(address())
    assert len(calls(http, "grant_type=refresh_token")) == 1


async def test_a_refresh_that_is_rejected_is_provider_down():
    http = FakeTransport(
        "instantedumail",
        routes(
            **{
                LIST: Reply(401, load("jwt_expired.json")),
                REFRESH: Reply(400, {"error": "invalid_grant"}),
            }
        ),
    )
    with pytest.raises(ProviderDown):
        await InstantEduMail(http).list(address())
