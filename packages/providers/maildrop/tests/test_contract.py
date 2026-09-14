import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, ProviderDown, SchemaDrift
from cowbird.models import Address
from cowbird.testing import FakeTransport
from cowbird_maildrop import DOMAIN, Maildrop

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {"inbox(": load("inbox.json"), "message(": load("message.json"), **overrides}


ADDRESS = Address(f"cbfixture@{DOMAIN}", "maildrop")


class TestMaildropContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return Maildrop(FakeTransport("maildrop", routes()))


async def test_the_mailbox_is_a_variable_not_part_of_the_query():
    http = FakeTransport("maildrop", routes())
    await Maildrop(http).list(ADDRESS)
    body = http.seen[-1][2]["json"]
    assert body["variables"] == {"mailbox": "cbfixture"}
    assert "cbfixture" not in body["query"]


async def test_a_null_message_is_gone():
    http = FakeTransport("maildrop", routes(**{"message(": load("message_null.json")}))
    with pytest.raises(MessageGone):
        await Maildrop(http).get(ADDRESS, "gYz1WaK7aq")


async def test_graphql_errors_are_provider_down():
    http = FakeTransport("maildrop", routes(**{"inbox(": load("errors.json")}))
    with pytest.raises(ProviderDown):
        await Maildrop(http).list(ADDRESS)


async def test_non_dict_row_in_list_raises_schema_drift():
    http = FakeTransport(
        "maildrop", routes(**{"inbox(": {"data": {"inbox": ["not a dict"]}}})
    )
    with pytest.raises(SchemaDrift):
        await Maildrop(http).list(ADDRESS)


async def test_non_dict_message_raises_schema_drift():
    http = FakeTransport(
        "maildrop", routes(**{"message(": {"data": {"message": "not a dict"}}})
    )
    with pytest.raises(SchemaDrift):
        await Maildrop(http).get(ADDRESS, "gYz1WaK7aq")


async def test_the_text_comes_from_the_html_not_the_raw_source():
    message = await Maildrop(FakeTransport("maildrop", routes())).get(ADDRESS, "gYz1WaK7aq")
    assert message.text.startswith("Your verification code is 482913")
    assert "Received:" not in message.text
