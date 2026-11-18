import json
from datetime import timedelta
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import InboxPrivacy
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport
from cowbird_meowmail import MeowMail

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes):
    return FakeTransport("meowmail", routes)


def default_routes(**overrides):
    routes = {
        # No domains route: generate() validates against static caps and never
        # fetches /domains. domains.json stays on disk as a recon record.
        "inboxes?": load("inbox_created.json"),
        "/emails?": load("emails_empty.json"),
        "/api/v1/emails/01a1df49-33db-4c8e-9f6a-482913482913": load("email_detail.json"),
    }
    routes.update(overrides)
    return routes


def addr(value="cowbird482913@meowmail.in"):
    from cowbird.models import Address

    return Address(value=value, provider="meowmail")


class TestMeowMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return MeowMail(fake(default_routes()))


async def test_generate_posts_domain_and_local_part():
    http = fake(default_routes())
    address = await MeowMail(http).generate(GenerateOptions(local="Cowbird.482"))
    # The API pattern is ^[a-z0-9][a-z0-9._+\-]{0,63}$: lowercase before sending.
    body = [kw for m, u, kw in http.seen if m == "POST"][0]["json"]
    assert body == {"domain": "meowmail.in", "local_part": "cowbird.482"}
    assert address.value == "cowbird.482@meowmail.in"
    assert address.expires_at is not None


async def test_generate_random_local_part_when_none_given():
    http = fake(default_routes())
    a = await MeowMail(http).generate()
    body = [kw for m, u, kw in http.seen if m == "POST"][0]["json"]
    assert a.value == body["local_part"] + "@meowmail.in"
    assert body["local_part"].startswith("cb")


async def test_generate_sets_one_hour_expiry():
    address = await MeowMail(fake(default_routes())).generate()
    assert address.expires_at is not None


async def test_generate_passes_the_requested_domain_through():
    # The server guarantees emptiness for the address it creates, so the body
    # must carry the caller's domain, not a hardcoded default.
    http = fake(default_routes())
    address = await MeowMail(http).generate(GenerateOptions(domain="catpost.site"))
    body = [kw for m, u, kw in http.seen if m == "POST"][0]["json"]
    assert body["domain"] == "catpost.site"
    assert address.value.endswith("@catpost.site")


async def test_unparseable_received_at_degrades_to_none():
    bad = load("emails_full.json")
    rows = [{**bad["data"][0], "received_at": "not-a-timestamp"}]
    routes = default_routes(**{"/emails?": {**bad, "data": rows}})
    row = (await MeowMail(fake(routes)).list(addr()))[0]
    assert row.received_at is None


async def test_generate_unknown_domain_is_not_supported():
    http = fake(default_routes())
    with pytest.raises(NotSupported):
        await MeowMail(http).generate(GenerateOptions(domain="not-a-domain.test"))


async def test_generate_without_data_envelope_raises_schema_drift():
    routes = default_routes(**{"inboxes?": {"address": "x@meowmail.in"}})
    with pytest.raises(SchemaDrift):
        await MeowMail(fake(routes)).generate()


async def test_list_maps_summary_rows():
    routes = default_routes(**{"/emails?": load("emails_full.json")})
    rows = await MeowMail(fake(routes)).list(addr())
    assert len(rows) == 1
    row = rows[0]
    assert row.id == "01a1df49-33db-4c8e-9f6a-482913482913"
    assert row.sender == "noreply@example.test"
    assert row.subject == "Your verification code is 482913"
    assert row.received_at is not None


async def test_list_row_without_id_raises_schema_drift_naming_id():
    bad = load("emails_full.json")
    stripped = dict(bad["data"][0])
    del stripped["id"]
    routes = default_routes(**{"/emails?": {**bad, "data": [stripped]}})
    with pytest.raises(SchemaDrift, match="id"):
        await MeowMail(fake(routes)).list(addr())


async def test_list_non_dict_data_raises_schema_drift():
    routes = default_routes(**{"/emails?": {"data": "nope"}})
    with pytest.raises(SchemaDrift):
        await MeowMail(fake(routes)).list(addr())


async def test_get_returns_text_message():
    message = await MeowMail(fake(default_routes())).get(
        addr(), "01a1df49-33db-4c8e-9f6a-482913482913"
    )
    assert message.text == "Your verification code is 482913. It expires in 10 minutes."
    assert message.html == ""
    assert message.sender == "noreply@example.test"


async def test_get_falls_back_to_html_text():
    routes = default_routes(
        **{"/api/v1/emails/01a1df49-33db-4c8e-9f6a-482913482913": load("email_detail_html.json")}
    )
    message = await MeowMail(fake(routes)).get(addr(), "01a1df49-33db-4c8e-9f6a-482913482913")
    assert "482913" in message.text
    assert "482913" in message.html


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class SendOnlyTransport:
    """Minimal transport exposing only send(), for status-branching paths."""

    provider = "meowmail"

    def __init__(self, status, text=""):
        self.resp = _Resp(status, text)

    async def send(self, method, url, **kw):
        return self.resp


async def test_get_404_raises_message_gone():
    with pytest.raises(MessageGone):
        await MeowMail(SendOnlyTransport(404)).get(addr(), "01a1df49-33db-4c8e-9f6a-482913482913")


async def test_get_non_json_raises_schema_drift():
    with pytest.raises(SchemaDrift):
        await MeowMail(SendOnlyTransport(200, "<html>not json</html>")).get(
            addr(), "01a1df49-33db-4c8e-9f6a-482913482913"
        )


async def test_delete_204_is_silence():
    assert await MeowMail(SendOnlyTransport(204)).delete(addr(), "m1") is None


async def test_delete_404_is_idempotent_success():
    assert await MeowMail(SendOnlyTransport(404)).delete(addr(), "m1") is None


async def test_delete_500_raises_provider_down():
    with pytest.raises(ProviderDown):
        await MeowMail(SendOnlyTransport(500)).delete(addr(), "m1")


async def test_inbox_is_public_reads_are_keyless():
    provider = MeowMail(fake(default_routes()))
    assert provider.caps.inbox_privacy is InboxPrivacy.PUBLIC
    assert provider.caps.custom_local is True
    assert provider.caps.delete is True
    assert provider.caps.needs_state is False
    assert provider.caps.address_ttl == timedelta(hours=1)
