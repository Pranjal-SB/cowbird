import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import AddressExpired, MessageGone, ProviderDown, SchemaDrift
from cowbird.models import Address
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_nicemail import NiceMail

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "20260911T092413-3854"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3ODkyMDAwMDB9.Zml4dHVyZXNpZ25hdHVyZQ"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "https://nicemail.cc/": (FIXTURES / "home.html").read_text(encoding="utf-8"),
        "/api/v1/mailbox/": load("mailbox.json"),
        f"/{MESSAGE_ID}": load("message.json"),
        **overrides,
    }


ADDRESS = Address("cbfixture@suarj.com", "nicemail")


class TestNiceMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return NiceMail(FakeTransport("nicemail", routes()))


async def test_the_page_token_is_scraped_once_and_reused():
    http = FakeTransport("nicemail", routes())
    provider = NiceMail(http)
    await provider.list(ADDRESS)
    await provider.list(ADDRESS)
    assert sum("nicemail.cc" in url for _, url, _ in http.seen) == 1
    assert http.seen[-1][2]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert {"X-Request-ID", "X-Timestamp"} <= http.seen[-1][2]["headers"].keys()


async def test_a_401_rescrapes_the_token_and_retries_once():
    listing = Responses(Reply(401, "Unauthorized"), Reply(200, load("mailbox.json")))
    http = FakeTransport("nicemail", routes(**{"/api/v1/mailbox/": listing}))
    rows = await NiceMail(http).list(ADDRESS)
    assert [r.id for r in rows] == [MESSAGE_ID]
    assert sum("nicemail.cc" in url for _, url, _ in http.seen) == 2


async def test_a_404_on_read_is_gone():
    http = FakeTransport("nicemail", routes(**{f"/{MESSAGE_ID}": Reply(404, "not found")}))
    with pytest.raises(MessageGone):
        await NiceMail(http).get(ADDRESS, MESSAGE_ID)


async def test_the_body_text_is_used_as_sent():
    message = await NiceMail(FakeTransport("nicemail", routes())).get(ADDRESS, MESSAGE_ID)
    assert message.text == "Your verification code is 482913."


async def test_a_404_on_the_mailbox_expires_the_address_not_the_message():
    http = FakeTransport("nicemail", routes(**{"/api/v1/mailbox/": Reply(404, "not found")}))
    with pytest.raises(AddressExpired):
        await NiceMail(http).list(ADDRESS)


async def test_a_non_dict_row_in_the_mailbox_list_raises_schema_drift():
    http = FakeTransport("nicemail", routes(**{"/api/v1/mailbox/": ["not-a-row"]}))
    with pytest.raises(SchemaDrift):
        await NiceMail(http).list(ADDRESS)


async def test_a_403_on_the_token_page_does_not_quarantine():
    # A WAF/maintenance page on nicemail.cc's homepage has no JWT in it; that
    # must not be indistinguishable from "they removed the Nuxt payload".
    http = FakeTransport(
        "nicemail", routes(**{"https://nicemail.cc/": Reply(403, "<html>blocked</html>")})
    )
    with pytest.raises(ProviderDown):
        await NiceMail(http).list(ADDRESS)


async def test_a_truthy_non_dict_body_raises_schema_drift():
    message = load("message.json")
    message["body"] = "not-an-object"
    http = FakeTransport("nicemail", routes(**{f"/{MESSAGE_ID}": message}))
    with pytest.raises(SchemaDrift):
        await NiceMail(http).get(ADDRESS, MESSAGE_ID)
