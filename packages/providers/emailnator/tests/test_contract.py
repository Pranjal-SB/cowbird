import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageLocked, ProviderDown, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport
from cowbird_emailnator import Emailnator

FIXTURES = Path(__file__).parent / "fixtures"
ADDRESS = Address("cb.fixture.first@gmail.com", "emailnator")


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes, status=200):
    return FakeTransport("emailnator", routes, status)


DEFAULT_ROUTES = {
    "/api/generate-email": load("generate.json"),
    "/api/message-list": load("message_list_full.json"),
    "/api/message/": load("message_raw.json"),
    "/api/delete-message/": "",
}


class TestEmailnatorContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return Emailnator(fake(dict(DEFAULT_ROUTES)))


async def test_generate_requests_the_dot_gmail_option():
    # The option id is the whole request. Sending the wrong one silently yields
    # a different class of address than the capability record promises.
    http = fake(dict(DEFAULT_ROUTES))
    address = await Emailnator(http).generate(GenerateOptions())
    method, url, kw = http.seen[0]
    body = kw.get("json")
    assert (method, body) == ("POST", {"ids": [3]})
    assert url.endswith("/api/generate-email")
    assert address.value == "cb.fixture.first@gmail.com"
    assert address.provider == "emailnator"


async def test_generate_without_an_email_string_raises_schema_drift():
    http = fake({**DEFAULT_ROUTES, "/api/generate-email": {"status": "success"}})
    with pytest.raises(SchemaDrift, match="email"):
        await Emailnator(http).generate(GenerateOptions())


async def test_an_address_upstream_has_never_seen_is_an_empty_inbox_not_an_error():
    # emailnator answers 404 for an unknown address, which is where every
    # freshly generated address starts. Treating that as a fault would mark the
    # provider down on its own happy path.
    http = fake(dict(DEFAULT_ROUTES), status=404)
    assert await Emailnator(http).list(ADDRESS) == []


async def test_message_row_missing_id_raises_schema_drift_naming_id():
    broken = {"status": "success", "messages": [{"from": "a@b.test", "subject": "hi"}]}
    http = fake({**DEFAULT_ROUTES, "/api/message-list": broken})
    with pytest.raises(SchemaDrift, match="id"):
        await Emailnator(http).list(ADDRESS)


async def test_message_list_in_an_unexpected_shape_raises_schema_drift():
    http = fake({**DEFAULT_ROUTES, "/api/message-list": {"status": "success"}})
    with pytest.raises(SchemaDrift, match="messages"):
        await Emailnator(http).list(ADDRESS)


async def test_row_timestamps_are_unix_epochs_not_iso_strings():
    # Recorded rows carry integer epoch seconds. An ISO parse silently yields
    # None for every message, which loses ordering without failing anything.
    rows = await Emailnator(fake(dict(DEFAULT_ROUTES))).list(ADDRESS)
    assert rows[0].received_at == datetime.fromtimestamp(1788705267, UTC)


async def test_get_carries_the_envelope_through_from_the_document():
    message = await Emailnator(fake(dict(DEFAULT_ROUTES))).get(ADDRESS, "gp1.Ty1XDO")
    assert message.sender == "Digen AI <welcome@digen.ai>"
    assert message.subject == "Verify your DIGEN email address"
    assert "270442" in message.text


async def test_a_paywalled_message_raises_message_locked_and_never_reroutes():
    # MessageLocked is an answer to the caller, not a provider fault: rerouting
    # would try every backend in the fleet for a message only this one holds.
    locked = {"id": "x", "content": "", "locked": True}
    http = fake({**DEFAULT_ROUTES, "/api/message/": locked})
    with pytest.raises(MessageLocked) as caught:
        await Emailnator(http).get(ADDRESS, "x")
    assert caught.value.reroutable is False


async def test_get_without_content_raises_schema_drift():
    http = fake({**DEFAULT_ROUTES, "/api/message/": {"id": "x"}})
    with pytest.raises(SchemaDrift, match="content"):
        await Emailnator(http).get(ADDRESS, "x")


async def test_deleting_an_already_deleted_message_is_success():
    http = fake(dict(DEFAULT_ROUTES), status=404)
    await Emailnator(http).delete(ADDRESS, "gone")


async def test_delete_reports_an_unexpected_status_as_provider_down():
    http = fake(dict(DEFAULT_ROUTES), status=403)
    with pytest.raises(ProviderDown, match="403"):
        await Emailnator(http).delete(ADDRESS, "x")
