import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, SchemaDrift
from cowbird.models import Address
from cowbird.testing import FakeTransport, Responses
from cowbird_tempmailio import TempMailIo

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "df254d10-f1fd-490d-849f-72da92e86038"
ADDRESS = Address("8xsmyjdem5@ruutukf.com", "tempmailio")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "POST https://api.internal.temp-mail.io/api/v3/email/new": Responses(
            load("new_1.json"), load("new_2.json")
        ),
        "/messages": load("messages.json"),
        **overrides,
    }


class TestTempMailIoContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TempMailIo(FakeTransport("tempmailio", routes()))


async def test_the_body_comes_inline_from_the_list():
    message = await TempMailIo(FakeTransport("tempmailio", routes())).get(ADDRESS, MESSAGE_ID)
    assert "everything is working as expected" in message.text
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)
    assert message.sender == '"JoltMx Delivery Test" <test@sendtest.joltmx.com>'


async def test_a_message_no_longer_listed_is_gone():
    http = FakeTransport("tempmailio", routes(**{"/messages": load("messages_empty.json")}))
    with pytest.raises(MessageGone):
        await TempMailIo(http).get(ADDRESS, MESSAGE_ID)


async def test_a_list_that_is_not_an_array_is_drift():
    http = FakeTransport("tempmailio", routes(**{"/messages": {"error": "changed"}}))
    with pytest.raises(SchemaDrift):
        await TempMailIo(http).list(ADDRESS)
