import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply
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


async def test_a_404_from_getHtml_with_list_still_answering_is_gone():
    # Mailgun storage expires and returns 404, but the row still lists.
    http = FakeTransport(
        "inboxkitten", routes(**{"/mail/getHtml": Reply(status=404, payload="Not Found")})
    )
    with pytest.raises(MessageGone):
        await InboxKitten(http).get(ADDRESS, MESSAGE_ID)


async def test_a_404_from_getInfo_with_list_still_answering_is_gone():
    # Mailgun storage expires and returns 404 on info, but the row still lists.
    http = FakeTransport(
        "inboxkitten", routes(**{"/mail/getInfo": Reply(status=404, payload="Not Found")})
    )
    with pytest.raises(MessageGone):
        await InboxKitten(http).get(ADDRESS, MESSAGE_ID)


async def test_a_404_from_read_while_list_is_down_propagates_the_list_failure():
    # Both list and read fail; list failure propagates, not treated as MessageGone.
    down = ProviderDown("inboxkitten: HTTP 502")
    http = FakeTransport(
        "inboxkitten",
        routes(
            **{"/mail/getHtml": Reply(status=404, payload="Not Found"), "/mail/list": down}
        ),
    )
    with pytest.raises(ProviderDown):
        await InboxKitten(http).get(ADDRESS, MESSAGE_ID)


async def test_error_page_is_not_returned_as_message_body():
    # Ensure that a 4xx response body is never parsed into a Message.
    http = FakeTransport(
        "inboxkitten", routes(**{"/mail/getHtml": Reply(status=500, payload="Error occurred")})
    )
    # Should raise MessageGone (after probing list), not return a Message with error text.
    with pytest.raises(MessageGone):
        msg = await InboxKitten(http).get(ADDRESS, MESSAGE_ID)
        # This should never execute, but if it did, we'd confirm the body is not "Error occurred".
        assert "Error occurred" not in msg.html


async def test_non_json_body_from_getInfo_raises_schema_drift():
    # A 200 from /getInfo with non-JSON body is upstream drift, not a parse error.
    http = FakeTransport(
        "inboxkitten", routes(**{"/mail/getInfo": Reply(status=200, payload="<html>Error</html>")})
    )
    with pytest.raises(SchemaDrift):
        await InboxKitten(http).get(ADDRESS, MESSAGE_ID)
