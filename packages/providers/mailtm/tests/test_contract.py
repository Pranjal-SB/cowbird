import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply
from cowbird_mailtm import MailTm

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes, status=200):
    return FakeTransport("mailtm", routes, status)


DEFAULT_ROUTES = {
    "/domains": load("domains.json"),
    "/accounts": load("account.json"),
    "/token": load("token.json"),
    "/messages": load("messages_full.json"),
    "/messages/6a1f2c9e4b7d": load("message_one.json"),
}


class TestMailTmContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return MailTm(fake(dict(DEFAULT_ROUTES)))


async def test_generate_creates_an_account_then_exchanges_it_for_a_token():
    http = fake(dict(DEFAULT_ROUTES))
    address = await MailTm(http).generate(GenerateOptions())
    assert [m for m, *_ in http.seen] == ["GET", "POST", "POST"]
    assert address.provider == "mailtm"
    # The credentials travel with the address so an expiring JWT can be re-minted.
    packed = json.loads(address.state)
    assert packed["token"] and packed["address"] == address.value and packed["password"]


async def test_non_list_domains_response_raises_schema_drift():
    # mail.tm's list endpoints return a plain JSON array under the
    # Accept: application/json header Transport.json() always sends. Anything
    # else (e.g. an envelope dict) means the shape moved.
    http = fake({**DEFAULT_ROUTES, "/domains": {"hydra:member": []}})
    with pytest.raises(SchemaDrift):
        await MailTm(http).generate(GenerateOptions())


async def test_message_row_missing_id_raises_schema_drift_naming_id():
    # A renamed/dropped `id` field must quarantine with a diagnosable message,
    # not a bare KeyError (which registry.py would only see as "provider down").
    row_without_id = {
        "from": {"address": "noreply@example.test"},
        "subject": "Verify your account",
        "createdAt": "2026-09-05T10:12:03+00:00",
    }
    http = fake({**DEFAULT_ROUTES, "/messages": [row_without_id]})
    with pytest.raises(SchemaDrift, match="id"):
        await MailTm(http).list(_address_with_token())


async def test_a_requested_domain_the_provider_does_not_serve_is_refused():
    # NotSupported, not SchemaDrift: a caller typo must not quarantine a healthy
    # provider. NotSupported is also not reroutable, so the pool surfaces it.
    http = fake(dict(DEFAULT_ROUTES))
    with pytest.raises(NotSupported):
        await MailTm(http).generate(GenerateOptions(domain="not-a-real-domain.test"))


def _address_with_token():
    from cowbird.models import Address

    return Address(value="a@b.test", provider="mailtm", state=json.dumps(
        {"address": "a@b.test", "password": "x", "token": "t"}
    ))


class FakeStatusResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeSendTransport:
    """A minimal fake exposing only send(), for delete()'s status-branching."""

    provider = "mailtm"

    def __init__(self, status_code):
        self.status_code = status_code

    async def send(self, method, url, **kw):
        return FakeStatusResponse(self.status_code)


async def test_delete_on_204_returns_none():
    result = await MailTm(FakeSendTransport(204)).delete(_address_with_token(), "m1")
    assert result is None


async def test_delete_on_404_is_treated_as_already_gone():
    # Idempotency, not an oversight: the caller wanted the message gone, and
    # it's gone. Deleting twice must not raise.
    result = await MailTm(FakeSendTransport(404)).delete(_address_with_token(), "m1")
    assert result is None


async def test_delete_on_403_raises_provider_down():
    with pytest.raises(ProviderDown):
        await MailTm(FakeSendTransport(403)).delete(_address_with_token(), "m1")


async def test_delete_on_500_raises_provider_down_via_retry_path():
    class FailingTransport:
        provider = "mailtm"

        async def send(self, method, url, **kw):
            raise ProviderDown("mailtm: HTTP 500")

    with pytest.raises(ProviderDown):
        await MailTm(FailingTransport()).delete(_address_with_token(), "m1")


async def test_a_message_that_has_expired_is_gone_not_drift():
    # mail.tm answers an expired or deleted message with a 404 JSON error.
    # That is the message's fault, not the backend's, so it must not count
    # against the provider's health.
    missing = Reply(404, {"code": 404, "message": "Not Found"})
    http = fake({**DEFAULT_ROUTES, "/messages/6a1f2c9e4b7d": missing})
    with pytest.raises(MessageGone):
        await MailTm(http).get(_address_with_token(), "6a1f2c9e4b7d")
