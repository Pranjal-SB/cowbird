import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport
from cowbird_tempmailplus import TempMailPlus

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "1880235496"
ADDRESS = Address("cbdde6e6ee7d@mailto.plus", "tempmailplus")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "GET https://tempmail.plus/api/mails?": load("mails.json"),
        f"GET https://tempmail.plus/api/mails/{MESSAGE_ID}": load("mail.json"),
        "DELETE https://tempmail.plus/api/mails/": {"result": True},
        **overrides,
    }


class TestTempMailPlusContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TempMailPlus(FakeTransport("tempmailplus", routes()))


async def test_the_inbox_is_read_by_address():
    http = FakeTransport("tempmailplus", routes())
    rows = await TempMailPlus(http).list(ADDRESS)
    assert [r.id for r in rows] == [MESSAGE_ID]
    assert rows[0].subject == "JoltMx test email (ref 01a0d345ccdd)"
    assert any((kw.get("params") or {}).get("email") == ADDRESS.value for _, _, kw in http.seen)


async def test_an_empty_inbox_is_an_empty_list():
    http = FakeTransport(
        "tempmailplus", routes(**{"GET https://tempmail.plus/api/mails?": load("mails_empty.json")})
    )
    assert await TempMailPlus(http).list(ADDRESS) == []


async def test_get_reads_the_body_and_the_utc_date():
    message = await TempMailPlus(FakeTransport("tempmailplus", routes())).get(ADDRESS, MESSAGE_ID)
    assert message.sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert "everything is working as expected" in message.text
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)
    assert message.received_at == datetime(2026, 9, 24, 11, 56, 2, tzinfo=UTC)


async def test_a_message_the_box_does_not_hold_is_gone():
    http = FakeTransport(
        "tempmailplus",
        routes(**{f"GET https://tempmail.plus/api/mails/{MESSAGE_ID}": load("mail_missing.json")}),
    )
    with pytest.raises(MessageGone):
        await TempMailPlus(http).get(ADDRESS, MESSAGE_ID)


async def test_a_list_without_mail_list_is_drift():
    http = FakeTransport(
        "tempmailplus", routes(**{"GET https://tempmail.plus/api/mails?": {"x": 1}})
    )
    with pytest.raises(SchemaDrift):
        await TempMailPlus(http).list(ADDRESS)


async def test_delete_sends_the_address_with_the_id():
    http = FakeTransport("tempmailplus", routes())
    await TempMailPlus(http).delete(ADDRESS, MESSAGE_ID)
    method, url, kw = http.seen[-1]
    assert method == "DELETE" and url.endswith(f"/api/mails/{MESSAGE_ID}")
    assert kw["data"]["email"] == ADDRESS.value


async def test_generate_honours_a_chosen_local_and_domain():
    address = await TempMailPlus(FakeTransport("tempmailplus", routes())).generate(
        GenerateOptions(local="hello", domain="fexpost.com")
    )
    assert address.value == "hello@fexpost.com"


async def test_a_domain_it_does_not_serve_is_refused():
    with pytest.raises(NotSupported):
        await TempMailPlus(FakeTransport("tempmailplus", routes())).generate(
            GenerateOptions(domain="gmail.com")
        )
