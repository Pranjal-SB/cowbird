import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.testing import FakeTransport, Responses
from cowbird_temporarymail import TemporaryMail

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "3XnhI0W1FLu9lX4qWlGCkrkAoIJwx23q"
ADDRESS = Address(
    "Polo.Pascal@HorizonsPost.com", "temporarymail", state="5zvhLspSb3KmQjY0BddngDj8oEHyP87e"
)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "action=requestEmailAccess": Responses(load("access_1.json"), load("access_2.json")),
        "action=checkInbox": load("inbox.json"),
        "action=getEmail": load("email.json"),
        "/view/": (FIXTURES / "view.html").read_text(encoding="utf-8"),
        **overrides,
    }


class TestTemporaryMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TemporaryMail(FakeTransport("temporarymail", routes()))


async def test_the_inbox_is_read_with_the_secret_key_from_generate():
    http = FakeTransport("temporarymail", routes())
    provider = TemporaryMail(http)
    address = await provider.generate()
    await provider.list(address)
    assert any(
        (kw.get("params") or {}).get("value") == address.state for _, _, kw in http.seen
    )


async def test_an_empty_inbox_answers_an_empty_array():
    http = FakeTransport("temporarymail", routes(**{"action=checkInbox": []}))
    assert await TemporaryMail(http).list(ADDRESS) == []


async def test_get_takes_the_subject_from_get_email_and_the_body_from_view():
    # The inbox listing says "[No Subject]" until a message has been opened.
    message = await TemporaryMail(FakeTransport("temporarymail", routes())).get(
        ADDRESS, MESSAGE_ID
    )
    assert message.subject.startswith("JoltMx test email")
    assert "everything is working as expected" in message.text
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)


async def test_a_message_get_email_does_not_know_is_gone():
    http = FakeTransport("temporarymail", routes(**{"action=getEmail": []}))
    with pytest.raises(MessageGone):
        await TemporaryMail(http).get(ADDRESS, MESSAGE_ID)


async def test_list_without_the_secret_key_is_refused():
    with pytest.raises(NotSupported):
        await TemporaryMail(FakeTransport("temporarymail", routes())).list(
            Address("x@HorizonsPost.com", "temporarymail")
        )


async def test_an_inbox_that_is_neither_object_nor_array_is_drift():
    http = FakeTransport("temporarymail", routes(**{"action=checkInbox": "changed"}))
    with pytest.raises(SchemaDrift):
        await TemporaryMail(http).list(ADDRESS)
