import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, SchemaDrift
from cowbird.models import Address
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_tenminutemail import TenMinuteMail

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "/session/address": Responses(
            Reply(200, load("address.json"), cookies={"JSESSIONID": "fixture-session-a"}),
            Reply(200, load("address_second.json"), cookies={"JSESSIONID": "fixture-session-b"}),
        ),
        "/messages/messagesAfter/0": load("messages.json"),
        **overrides,
    }


ADDRESS = Address("cbfixtureaaaa@onldm.net", "10minutemail", state="fixture-session-a")


class TestTenMinuteMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TenMinuteMail(FakeTransport("10minutemail", routes()))


def test_it_asks_for_a_fresh_session_per_request():
    assert TenMinuteMail.caps.fresh_session is True


async def test_the_session_cookie_is_the_state_and_is_replayed():
    http = FakeTransport("10minutemail", routes())
    provider = TenMinuteMail(http)
    address = await provider.generate()
    assert address.state == "fixture-session-a"
    await provider.list(address)
    assert http.seen[-1][2]["cookies"] == {"JSESSIONID": "fixture-session-a"}


async def test_an_address_without_a_session_cookie_is_drift():
    http = FakeTransport("10minutemail", routes(**{"/session/address": load("address.json")}))
    with pytest.raises(SchemaDrift, match="JSESSIONID"):
        await TenMinuteMail(http).generate()


async def test_the_address_expires_in_ten_minutes():
    address = await TenMinuteMail(FakeTransport("10minutemail", routes())).generate()
    remaining = address.expires_at - datetime.now(UTC)
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)


async def test_the_body_is_read_from_the_list_row():
    message = await TenMinuteMail(FakeTransport("10minutemail", routes())).get(
        ADDRESS, "-1999566867-1570421277"
    )
    assert message.text == "Your verification code is 482913."
    assert message.links == ("https://example.test/verify?token=abc",)


async def test_an_id_no_longer_listed_is_gone():
    with pytest.raises(MessageGone):
        await TenMinuteMail(FakeTransport("10minutemail", routes())).get(ADDRESS, "nope")
