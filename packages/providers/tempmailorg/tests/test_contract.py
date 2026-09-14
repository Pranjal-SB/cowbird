import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import AddressExpired, MessageGone, ProviderDown, SchemaDrift
from cowbird.models import Address
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_tempmailorg import TempMailOrg

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "6aa3cf8112e593e42adf0df7"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "/mailbox": Responses(load("mailbox.json"), load("mailbox_second.json")),
        "/messages": load("messages.json"),
        f"/messages/{MESSAGE_ID}": load("message.json"),
        **overrides,
    }


ADDRESS = Address("cbfixturea@daugr.com", "tempmailorg", state="fixture.token.a")


class TestTempMailOrgContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TempMailOrg(FakeTransport("tempmailorg", routes()))


async def test_the_token_is_the_state_and_goes_out_as_a_bearer():
    http = FakeTransport("tempmailorg", routes())
    provider = TempMailOrg(http)
    address = await provider.generate()
    assert address.state == "fixture.token.a"
    await provider.list(address)
    assert http.seen[-1][2]["headers"]["Authorization"] == "Bearer fixture.token.a"


async def test_a_410_on_read_is_gone():
    http = FakeTransport(
        "tempmailorg",
        routes(**{f"/messages/{MESSAGE_ID}": Reply(410, {"errorName": "GoneException"})}),
    )
    with pytest.raises(MessageGone):
        await TempMailOrg(http).get(ADDRESS, MESSAGE_ID)


async def test_a_401_means_the_mailbox_is_gone():
    # The site's own client treats 401 as "make a new mailbox".
    http = FakeTransport(
        "tempmailorg", routes(**{"/messages": Reply(401, {"errorName": "UnauthorizedException"})})
    )
    with pytest.raises(AddressExpired):
        await TempMailOrg(http).list(ADDRESS)


async def test_the_html_body_is_turned_into_text():
    message = await TempMailOrg(FakeTransport("tempmailorg", routes())).get(ADDRESS, MESSAGE_ID)
    assert "482913" in message.text
    assert message.links == ("https://example.test/verify?token=abc",)


async def test_a_410_on_list_is_address_expired():
    # 410 from /messages means the mailbox is gone, not just a single message.
    http = FakeTransport(
        "tempmailorg", routes(**{"/messages": Reply(410, {"errorName": "GoneException"})})
    )
    with pytest.raises(AddressExpired):
        await TempMailOrg(http).list(ADDRESS)


async def test_a_403_on_generate_does_not_quarantine():
    # The per-IP mailbox limit can come back as an HTML error page; that must
    # not reach the JSON parser and quarantine a healthy provider.
    http = FakeTransport(
        "tempmailorg", routes(**{"/mailbox": Reply(403, "<html>Too many mailboxes</html>")})
    )
    with pytest.raises(ProviderDown):
        await TempMailOrg(http).generate()


async def test_a_non_dict_row_in_messages_raises_schema_drift():
    # If a row is not a dict, it should raise SchemaDrift, not TypeError.
    messages_with_bad_row = {"mailbox": "cbfixturea@daugr.com", "messages": ["not a dict"]}
    http = FakeTransport("tempmailorg", routes(**{"/messages": messages_with_bad_row}))
    with pytest.raises(SchemaDrift):
        await TempMailOrg(http).list(ADDRESS)
