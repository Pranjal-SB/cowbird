import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport
from cowbird_inboxkitten import DOMAIN, InboxKitten

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "us-west1:BAABAQfixturekey0001"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "/mail/list": load("list.json"),
        "/mail/getInfo": load("get_info.json"),
        "/mail/getHtml": (FIXTURES / "get_html.html").read_text(encoding="utf-8"),
        **overrides,
    }


ADDRESS = Address(f"cbfixture@{DOMAIN}", "inboxkitten")


class TestInboxKittenContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return InboxKitten(FakeTransport("inboxkitten", routes()))


async def test_generate_makes_no_request_and_honours_a_custom_local_part():
    http = FakeTransport("inboxkitten", routes())
    address = await InboxKitten(http).generate(GenerateOptions(local="chosen"))
    assert address.value == f"chosen@{DOMAIN}"
    assert http.seen == []


async def test_a_domain_it_does_not_serve_is_refused():
    with pytest.raises(NotSupported):
        await InboxKitten(FakeTransport("inboxkitten", routes())).generate(
            GenerateOptions(domain="elsewhere.test")
        )


async def test_the_message_id_carries_region_and_key():
    [row] = await InboxKitten(FakeTransport("inboxkitten", routes())).list(ADDRESS)
    assert row.id == MESSAGE_ID


async def test_a_failed_read_while_the_list_still_answers_is_gone():
    # Mailgun storage expires under a list row that still exists, and the read
    # then 500s. The backend is up; that one message is gone.
    http = FakeTransport(
        "inboxkitten", routes(**{"/mail/getHtml": ProviderDown("inboxkitten: HTTP 500")})
    )
    with pytest.raises(MessageGone):
        await InboxKitten(http).get(ADDRESS, MESSAGE_ID)


async def test_a_failed_read_while_the_list_is_down_too_is_the_provider():
    down = ProviderDown("inboxkitten: HTTP 502")
    http = FakeTransport("inboxkitten", routes(**{"/mail/getHtml": down, "/mail/list": down}))
    with pytest.raises(ProviderDown):
        await InboxKitten(http).get(ADDRESS, MESSAGE_ID)


async def test_an_id_without_a_region_is_refused():
    with pytest.raises(NotSupported):
        await InboxKitten(FakeTransport("inboxkitten", routes())).get(ADDRESS, "no-region")
