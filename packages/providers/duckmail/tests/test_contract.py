"""DuckMail replays through the mail.tm adapter against DuckMail's own fixtures.

domains/account/token/messages_empty were recorded live through a real
Transport. messages_full and message_one are hand-built in mail.tm's row shape:
recording them needs a message delivered to the inbox, and nothing here sends
mail.
"""

import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport
from cowbird_duckmail import DuckMail

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes):
    return FakeTransport("duckmail", routes)


DEFAULT_ROUTES = {
    "/domains": load("domains.json"),
    "/accounts": load("account.json"),
    "/token": load("token.json"),
    "/messages": load("messages_full.json"),
    "/messages/6a1f2c9e4b7d": load("message_one.json"),
}


class TestDuckMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return DuckMail(fake(dict(DEFAULT_ROUTES)))


def test_declares_its_own_name_and_front_doors():
    assert DuckMail.name == "duckmail"
    assert DuckMail.caps.sites == ("duckmail.sbs", "freetempmail.com")
    assert DuckMail.caps.domain_count == len(load("domains.json")["hydra:member"])


async def test_every_request_goes_to_duckmail_and_none_to_mail_tm():
    http = fake(dict(DEFAULT_ROUTES))
    provider = DuckMail(http)
    address = await provider.generate(GenerateOptions())
    rows = await provider.list(address)
    await provider.get(address, rows[0].id)
    await provider.delete(address, rows[0].id)
    urls = [url for _, url, _ in http.seen]
    assert len(urls) == 6
    assert all(url.startswith("https://api.duckmail.sbs/") for url in urls), urls


async def test_generate_uses_a_domain_duckmail_serves():
    address = await DuckMail(fake(dict(DEFAULT_ROUTES))).generate(GenerateOptions())
    served = {d["domain"] for d in load("domains.json")["hydra:member"]}
    assert address.value.split("@")[1] in served
    assert address.provider == "duckmail"


async def test_empty_inbox_envelope_lists_as_empty():
    http = fake({**DEFAULT_ROUTES, "/messages": load("messages_empty.json")})
    assert await DuckMail(http).list(_address()) == []


async def test_a_bare_array_where_the_envelope_belongs_is_schema_drift():
    # DuckMail wraps list endpoints in hydra:member even under
    # Accept: application/json. A bare array means the shape moved.
    http = fake({**DEFAULT_ROUTES, "/domains": []})
    with pytest.raises(SchemaDrift, match="hydra:member"):
        await DuckMail(http).generate(GenerateOptions())


def _address():
    state = json.dumps({"address": "a@niceground.shop", "password": "x", "token": "t"})
    return Address(value="a@niceground.shop", provider="duckmail", state=state)
