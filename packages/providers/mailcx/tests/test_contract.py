import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply
from cowbird_mailcx import MailCx

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "16ce0b8348a324de8720ac3c92ae8e6d"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "/v1/config": load("config.json"),
        "/v1/inbox/": Reply(200, load("inbox.json")),
        "/v1/email/": load("email.json"),
        **overrides,
    }


ADDRESS = Address("cbfixt@uqu.me", "mailcx")


class TestMailCxContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return MailCx(FakeTransport("mailcx", routes()))


async def test_generate_uses_the_default_domain_and_reads_config_once():
    http = FakeTransport("mailcx", routes())
    provider = MailCx(http)
    first = await provider.generate()
    await provider.generate()
    assert first.value.endswith("@uqu.me")
    assert sum("/v1/config" in url for _, url, _ in http.seen) == 1


async def test_a_domain_it_does_not_serve_is_refused():
    with pytest.raises(NotSupported):
        await MailCx(FakeTransport("mailcx", routes())).generate(
            GenerateOptions(domain="elsewhere.test")
        )


async def test_a_204_from_the_long_poll_is_an_empty_inbox():
    http = FakeTransport("mailcx", routes(**{"/v1/inbox/": Reply(204, "")}))
    assert await MailCx(http).list(ADDRESS) == []


async def test_a_404_on_read_is_gone():
    http = FakeTransport("mailcx", routes(**{"/v1/email/": Reply(404, {"error": "not_found"})}))
    with pytest.raises(MessageGone):
        await MailCx(http).get(ADDRESS, MESSAGE_ID)


async def test_the_text_body_is_used_as_sent():
    message = await MailCx(FakeTransport("mailcx", routes())).get(ADDRESS, MESSAGE_ID)
    assert message.text == "Your verification code is 482913."


async def test_a_non_dict_inbox_row_raises_schema_drift_not_type_error():
    payload = {"emails": ["not-a-dict"], "next_since": None}
    http = FakeTransport("mailcx", routes(**{"/v1/inbox/": Reply(200, payload)}))
    with pytest.raises(SchemaDrift):
        await MailCx(http).list(ADDRESS)


async def test_a_non_dict_message_body_raises_schema_drift_not_attribute_error():
    http = FakeTransport("mailcx", routes(**{"/v1/email/": Reply(200, ["not-a-dict"])}))
    with pytest.raises(SchemaDrift):
        await MailCx(http).get(ADDRESS, MESSAGE_ID)


async def test_non_dict_system_domains_entries_raise_schema_drift_not_type_error():
    config = {"system_domains": ["ddker.com", "uqu.me"], "ttl_seconds": 3600}
    http = FakeTransport("mailcx", routes(**{"/v1/config": config}))
    with pytest.raises(SchemaDrift):
        await MailCx(http).generate()


async def test_no_domain_field_anywhere_raises_schema_drift_not_index_error():
    config = {"system_domains": [{"not_domain": "ddker.com"}], "ttl_seconds": 3600}
    http = FakeTransport("mailcx", routes(**{"/v1/config": config}))
    with pytest.raises(SchemaDrift):
        await MailCx(http).generate()
