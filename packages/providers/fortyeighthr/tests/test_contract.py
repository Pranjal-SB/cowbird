"""Contract suite + parsing tests for the 48hr.email adapter.

48hr.email exposes a JSON API under /api/v1 (open source, CORS-open, no auth
for unlocked inboxes), so no HTML scraping is needed. Fixtures were recorded
through a real `Transport` against the official instance and neutralized:
senders are noreply@example.test, subjects/bodies carry the canonical
verification-code text.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, InboxPrivacy
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply
from cowbird_fortyeighthr import FortyEightHr

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes, status=200):
    return FakeTransport("fortyeighthr", routes, status)


DEFAULT_ROUTES = {
    "/config/domains": load("domains.json"),
    # catch-all for any generated address; the specific example@ key is
    # longer, so FakeTransport's longest-match prefers it where present.
    "/inbox/": load("inbox.json"),
    "/652266?": load("message.json"),
    "/inbox/example@48hr.email": load("inbox.json"),
    "/inbox/example@48hr.email/652266": load("message.json"),
}


def routes_with(**extra):
    merged = dict(DEFAULT_ROUTES)
    merged.update(extra)
    return merged


class TestFortyEightHrContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return FortyEightHr(fake(routes_with()))


async def test_generate_picks_a_served_domain_and_confirms_the_inbox():
    http = fake(routes_with())
    address = await FortyEightHr(http).generate(GenerateOptions())
    assert address.provider == "fortyeighthr"
    assert address.value.split("@")[1] in (
        "48hr.email", "a-germandu.de", "c1ph3r.xyz", "cloudflare.gay",
        "cutefrogs.xyz", "dontask.lol", "epstein-is.land", "free-tools.club",
        "fuck-ai.art", "gothgirl.pics", "hot-single.mom",
        "i-wont-read-your.email", "pride-worldwi.de", "sex-in-the.city",
        "used-condoms.shop",
    )
    # zero auth: no state to carry, the address is the only credential
    assert address.state is None
    assert [m for m, *_ in http.seen] == ["GET", "GET"]


async def test_generate_honors_requested_local_and_domain():
    http = fake(routes_with())
    address = await FortyEightHr(http).generate(
        GenerateOptions(local="myprobe", domain="48hr.email")
    )
    assert address.value == "myprobe@48hr.email"


async def test_a_requested_domain_the_provider_does_not_serve_is_refused():
    # NotSupported, not SchemaDrift: a caller typo must not quarantine a
    # healthy provider.
    http = fake(routes_with())
    with pytest.raises(NotSupported):
        await FortyEightHr(http).generate(GenerateOptions(domain="not-a-real-domain.test"))


async def test_list_parses_uid_sender_subject_date():
    rows = await FortyEightHr(fake(routes_with())).list(
        Address("example@48hr.email", "fortyeighthr")
    )
    assert [r.id for r in rows] == ["652266", "652263"]
    assert all(r.sender == "noreply@example.test" for r in rows)
    assert all(r.subject == "Your verification code is 482913." for r in rows)
    assert rows[0].received_at is not None


async def test_list_on_an_envelope_without_data_is_schema_drift():
    http = fake(routes_with(**{"/inbox/example@48hr.email": {"success": True}}))
    with pytest.raises(SchemaDrift):
        await FortyEightHr(http).list(Address("example@48hr.email", "fortyeighthr"))


async def test_list_row_missing_uid_raises_schema_drift_naming_uid():
    # A renamed/dropped `uid` field must quarantine with a diagnosable
    # message, not a bare KeyError.
    row = {
        "to": ["example@48hr.email"],
        "from": [{"address": "noreply@example.test", "name": "Example Sender"}],
        "date": "2026-11-15T10:12:03.000Z",
        "subject": "Your verification code is 482913.",
    }
    http = fake(routes_with(**{"/inbox/example@48hr.email": {"success": True, "data": [row]}}))
    with pytest.raises(SchemaDrift, match="uid"):
        await FortyEightHr(http).list(Address("example@48hr.email", "fortyeighthr"))


async def test_get_returns_the_full_message():
    message = await FortyEightHr(fake(routes_with())).get(
        Address("example@48hr.email", "fortyeighthr"), "652266"
    )
    assert message.id == "652266"
    assert message.sender == "noreply@example.test"
    assert message.subject == "Your verification code is 482913."
    assert "482913" in message.text
    assert "482913" in message.html
    assert "https://example.test/verify?a=1" in message.links


async def test_a_message_that_has_expired_is_gone_not_drift():
    # The API answers an expired or deleted message with a 404 JSON error.
    # That is the message's fault, not the backend's, so it must not count
    # against the provider's health.
    missing = Reply(404, {"success": False, "error": "Email not found", "code": "NOT_FOUND"})
    http = fake(routes_with(**{"/inbox/example@48hr.email/652266": missing}))
    with pytest.raises(MessageGone):
        await FortyEightHr(http).get(
            Address("example@48hr.email", "fortyeighthr"), "652266"
        )


class FakeStatusResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeSendTransport:
    """A minimal fake exposing only send(), for delete()'s status branching."""

    provider = "fortyeighthr"

    def __init__(self, status_code):
        self.status_code = status_code
        self.seen: list[tuple[str, str, dict]] = []

    async def send(self, method, url, **kw):
        self.seen.append((method, url, kw))
        return FakeStatusResponse(self.status_code)


async def test_delete_hits_the_web_delete_route_and_accepts_the_redirect():
    # Deletion is only exposed on the server-rendered side, as a GET that
    # 302s back to the inbox (curl_cffi follows it, so success is 200).
    http = FakeSendTransport(200)
    result = await FortyEightHr(http).delete(
        Address("example@48hr.email", "fortyeighthr"), "652266"
    )
    assert result is None
    assert http.seen == [(
        "GET", "https://48hr.email/inbox/example@48hr.email/652266/delete", {}
    )]


async def test_delete_derives_its_web_root_from_the_api_classvar():
    # A self-hosted deployment overrides `api` (and `web` if its mount
    # differs); delete must follow, not silently hit the official instance.
    class SelfHosted(FortyEightHr):
        api = "https://mail.selfhosted.test/api/v1"
        web = "https://mail.selfhosted.test"

    http = FakeSendTransport(200)
    await SelfHosted(http).delete(
        Address("a@selfhosted.test", "fortyeighthr"), "7"
    )
    assert http.seen[0][1] == "https://mail.selfhosted.test/inbox/a@selfhosted.test/7/delete"


async def test_delete_on_4xx_other_than_auth_is_provider_down():
    with pytest.raises(ProviderDown):
        await FortyEightHr(FakeSendTransport(403)).delete(
            Address("example@48hr.email", "fortyeighthr"), "652266"
        )


async def test_caps_match_the_service():
    caps = FortyEightHr.caps
    assert caps.inbox_privacy is InboxPrivacy.PUBLIC  # readable with zero cookies
    assert caps.address_ttl is None  # any local part works forever, no TTL
    assert caps.message_ttl == timedelta(days=2)  # 48h purge
    assert caps.custom_local is True
    assert caps.delete is True
    assert caps.needs_state is False
    assert caps.domain_count == 15
