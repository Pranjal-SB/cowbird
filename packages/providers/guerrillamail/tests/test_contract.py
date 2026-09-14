import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown
from cowbird.models import Address
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_guerrillamail import GuerrillaMail

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "f=get_email_address": Responses(
            load("get_email_address.json"), load("get_email_address_second.json")
        ),
        "f=check_email": load("check_email.json"),
        "f=fetch_email": load("fetch_email.json"),
        **overrides,
    }


ADDRESS = Address(
    "cbfixture01@guerrillamailblock.com", "guerrillamail", state="fixturesid0000000000000001"
)


class TestGuerrillaMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return GuerrillaMail(FakeTransport("guerrillamail", routes()))


def test_it_asks_for_a_fresh_session_per_request():
    # guerrillamail keys the inbox on PHPSESSID: on a shared jar the second
    # generate() returns the first address again (measured 2026-09-11).
    assert GuerrillaMail.caps.fresh_session is True


async def test_the_sid_token_travels_in_state_and_is_replayed():
    http = FakeTransport("guerrillamail", routes())
    provider = GuerrillaMail(http)
    address = await provider.generate()
    assert address.state == "fixturesid0000000000000001"
    await provider.list(address)
    assert http.seen[-1][2]["params"]["sid_token"] == "fixturesid0000000000000001"


async def test_the_body_comes_from_fetch_email_not_the_truncated_row():
    message = await GuerrillaMail(FakeTransport("guerrillamail", routes())).get(ADDRESS, "41")
    assert "482913" in message.text
    assert message.links == ("https://example.test/verify?token=abc",)


async def test_the_welcome_mails_zero_timestamp_is_no_timestamp():
    rows = {"list": [{**load("check_email.json")["list"][0], "mail_timestamp": 0}]}
    http = FakeTransport("guerrillamail", routes(**{"f=check_email": rows}))
    [row] = await GuerrillaMail(http).list(ADDRESS)
    assert row.received_at is None


async def test_an_unknown_message_is_gone():
    http = FakeTransport("guerrillamail", routes(**{"f=fetch_email": False}))
    with pytest.raises(MessageGone):
        await GuerrillaMail(http).get(ADDRESS, "999")


async def test_a_list_without_state_is_refused():
    with pytest.raises(NotSupported):
        await GuerrillaMail(FakeTransport("guerrillamail", routes())).list(
            Address("x@guerrillamailblock.com", "guerrillamail")
        )


async def test_a_403_error_page_on_list_does_not_quarantine():
    # An error page reaching the JSON parser used to raise SchemaDrift and
    # permanently quarantine the provider. A stale sid_token is an ordinary
    # error, not a shape change.
    http = FakeTransport(
        "guerrillamail", routes(**{"f=check_email": Reply(403, "<html>blocked</html>")})
    )
    with pytest.raises(AddressExpired):
        await GuerrillaMail(http).list(ADDRESS)


async def test_a_403_error_page_on_generate_does_not_quarantine():
    # 403, not 503: Transport turns a 5xx into ProviderDown before the adapter
    # sees it, so a 5xx here would pass with the status check removed. A 403
    # reaches the adapter, and generate() has no sid to call expired.
    http = FakeTransport(
        "guerrillamail", routes(**{"f=get_email_address": Reply(403, "<html>blocked</html>")})
    )
    with pytest.raises(ProviderDown):
        await GuerrillaMail(http).generate()
