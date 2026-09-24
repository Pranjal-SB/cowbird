import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, RateLimited, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_tempmailhub import TempMailHub

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://api.tempmailhub.org"
MESSAGE_ID = "imap-61417"
ADDRESS = Address("Josephgrant651@gmail.com", "tempmailhub")


# messages.json keeps the recorded row shape, but its content is synthetic:
# every inbox in the pool holds other people's mail.


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        f"POST {API}/emails?": Responses(load("generate_1.json"), load("generate_2.json")),
        f"POST {API}/emails/messages": load("messages.json"),
        **overrides,
    }


class TestTempMailHubContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TempMailHub(FakeTransport("tempmailhub", routes()))


async def test_generate_hands_out_a_gmail_address():
    address = await TempMailHub(FakeTransport("tempmailhub", routes())).generate()
    assert address.value == "cobbroy42@gmail.com"
    assert address.state is None


async def test_the_inbox_is_read_by_address():
    http = FakeTransport("tempmailhub", routes())
    rows = await TempMailHub(http).list(ADDRESS)
    assert [r.id for r in rows] == ["imap-61417", "imap-61416"]
    method, url, kw = http.seen[-1]
    assert (method, url) == ("POST", f"{API}/emails/messages")
    assert kw["json"] == {"email": ADDRESS.value}


async def test_list_rows_carry_sender_subject_and_utc_date():
    rows = await TempMailHub(FakeTransport("tempmailhub", routes())).list(ADDRESS)
    assert rows[0].sender == "Example <noreply@example.com>"
    assert rows[0].subject == "Your verification code"
    assert rows[0].received_at == datetime(2026, 9, 24, 12, 31, 42, tzinfo=UTC)


async def test_get_reads_the_row_body():
    message = await TempMailHub(FakeTransport("tempmailhub", routes())).get(ADDRESS, MESSAGE_ID)
    assert message.id == MESSAGE_ID
    assert message.sender == "Example <noreply@example.com>"
    assert message.subject == "Your verification code"
    assert message.received_at == datetime(2026, 9, 24, 12, 31, 42, tzinfo=UTC)
    assert "Your verification code is 482913" in message.text
    assert "<p" in message.html


async def test_get_extracts_links_from_the_body():
    message = await TempMailHub(FakeTransport("tempmailhub", routes())).get(ADDRESS, "imap-61416")
    assert any("example.com/confirm" in href for href in message.links)


async def test_a_message_no_longer_listed_is_gone():
    with pytest.raises(MessageGone):
        await TempMailHub(FakeTransport("tempmailhub", routes())).get(ADDRESS, "imap-1")


async def test_an_empty_inbox_is_an_empty_list():
    http = FakeTransport("tempmailhub", routes(**{f"POST {API}/emails/messages": {"emails": []}}))
    assert await TempMailHub(http).list(ADDRESS) == []


@pytest.mark.parametrize("payload", [{"emails": {}}, {"error": "x"}, [], {"emails": [{"x": 1}]}])
async def test_a_list_without_an_emails_array_is_drift(payload):
    http = FakeTransport("tempmailhub", routes(**{f"POST {API}/emails/messages": payload}))
    with pytest.raises(SchemaDrift):
        await TempMailHub(http).list(ADDRESS)


@pytest.mark.parametrize("payload", [{"email_id": 10}, {"email": "no-at-sign"}, []])
async def test_a_malformed_generate_is_drift(payload):
    http = FakeTransport("tempmailhub", routes(**{f"POST {API}/emails?": payload}))
    with pytest.raises(SchemaDrift):
        await TempMailHub(http).generate()


async def test_a_429_surfaces_as_rate_limited():
    http = FakeTransport("tempmailhub", routes(**{f"POST {API}/emails/messages": Reply(429, {})}))
    with pytest.raises(RateLimited):
        await TempMailHub(http).list(ADDRESS)


async def test_a_chosen_local_or_domain_is_refused():
    with pytest.raises(NotSupported):
        await TempMailHub(FakeTransport("tempmailhub", routes())).generate(
            GenerateOptions(local="me")
        )
