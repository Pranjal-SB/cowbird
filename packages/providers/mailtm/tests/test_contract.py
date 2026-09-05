import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import NotSupported, ProviderDown, SchemaDrift
from cowbird.provider import GenerateOptions
from cowbird_mailtm import MailTm

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeTransport:
    """Replays recorded upstream responses.

    Routes are matched longest-key-first, because `/messages/{id}` and
    `/messages` both appear in a single-message URL and the list fixture is the
    wrong answer for a body read.
    """

    provider = "mailtm"

    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    async def json(self, method, url, **kw):
        self.seen.append((method, url))
        for key in sorted(self.routes, key=len, reverse=True):
            if key in url:
                payload = self.routes[key]
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected request: {method} {url}")

    async def aclose(self):
        pass


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
        return MailTm(FakeTransport(dict(DEFAULT_ROUTES)))


async def test_generate_creates_an_account_then_exchanges_it_for_a_token():
    http = FakeTransport(dict(DEFAULT_ROUTES))
    address = await MailTm(http).generate(GenerateOptions())
    assert [m for m, _ in http.seen] == ["GET", "POST", "POST"]
    assert address.provider == "mailtm"
    # The credentials travel with the address so an expiring JWT can be re-minted.
    packed = json.loads(address.state)
    assert packed["token"] and packed["address"] == address.value and packed["password"]


async def test_non_list_domains_response_raises_schema_drift():
    # mail.tm's list endpoints return a plain JSON array under the
    # Accept: application/json header Transport.json() always sends. Anything
    # else (e.g. an envelope dict) means the shape moved.
    http = FakeTransport({**DEFAULT_ROUTES, "/domains": {"hydra:member": []}})
    with pytest.raises(SchemaDrift):
        await MailTm(http).generate(GenerateOptions())


async def test_a_requested_domain_the_provider_does_not_serve_is_refused():
    # NotSupported, not SchemaDrift: a caller typo must not quarantine a healthy
    # provider. NotSupported is also not reroutable, so the pool surfaces it.
    http = FakeTransport(dict(DEFAULT_ROUTES))
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
