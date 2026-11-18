"""10minutemail.net adapter tests (replay fixtures recorded 2026-11-15).

The backend is a PHP site keyed by the TENMM_G1_SESSION cookie. The transport
runs fresh_session (one throwaway session per request), so the adapter must
replay the cookie from Address.state on every call — the tests pin that.
"""

from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import SchemaDrift
from cowbird.models import Address, InboxPrivacy, Kind
from cowbird.testing import FakeTransport
from cowbird_tenminutemailnet import TenMinuteMailNet

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def fake(routes: dict) -> FakeTransport:
    return FakeTransport("tenminutemailnet", routes)


def default_routes() -> dict:
    from cowbird.testing import Reply, Responses

    cookies = {"TENMM_G1_SESSION": "917ad725ce86a0fab47987115dd19ef6", "lang": "en"}
    # The backend issues the address itself, so a replay of one recording can
    # only ever hand out one address; the second answer rotates it the way
    # /new.html would, keeping the contract's distinct-address test honest.
    home = Responses(
        Reply(payload=load("home.html"), cookies=cookies),
        Reply(payload=load("home.html").replace("user01@", "user02@"), cookies=cookies),
    )
    return {
        "GET https://10minutemail.net/?": home,
        "mailbox.ajax.php": load("mailbox.html"),
        "readmail.html?mid=welcome": load("readmail.html"),
    }


def _state(address: Address) -> dict:
    import json

    return json.loads(address.state)


class TestTenMinuteMailNetContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TenMinuteMailNet(fake(default_routes()))


async def test_generate_parses_address_and_stores_cookie_and_permalink():
    http = fake(default_routes())
    address = await TenMinuteMailNet(http).generate()
    assert address.value == "user01@zefvo.com"
    assert address.provider == "tenminutemailnet"
    assert address.expires_at is not None
    state = _state(address)
    assert state["cookie"] == "917ad725ce86a0fab47987115dd19ef6"
    assert state["permalink"] == "/permalink/user01@zefvo.com/PermA/PermbBBBBBBBBBBBBcdeUV/"
    assert state["key"] == "Key0Key0K0"


async def test_generate_replays_the_session_cookie_on_later_calls():
    # fresh_session transport: the cookie must travel in the request headers,
    # not in a session jar the transport throws away between requests.
    http = fake(default_routes())
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    await provider.list(address)
    list_call = [kw for _, url, kw in http.seen if "mailbox.ajax.php" in url][0]
    assert "TENMM_G1_SESSION=917ad725ce86a0fab47987115dd19ef6" in list_call["headers"]["Cookie"]


async def test_list_parses_the_welcome_mail_row():
    http = fake(default_routes())
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    rows = await provider.list(address)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == "welcome"
    assert row.sender == "noreply@example.test"
    assert row.subject  # subject text from the fixture row


async def test_list_on_an_empty_table_returns_an_empty_list():
    empty = '<table id="maillist"><tr><th>From</th><th>Subject</th><th>Date</th></tr></table>'
    http = fake({**default_routes(), "mailbox.ajax.php": empty})
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    assert await provider.list(address) == []


async def test_list_on_a_non_table_response_raises_schema_drift():
    http = fake({**default_routes(), "mailbox.ajax.php": "unexpected"})
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    with pytest.raises(SchemaDrift):
        await provider.list(address)


async def test_get_parses_sender_subject_and_body():
    http = fake(default_routes())
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    message = await provider.get(address, "welcome")
    assert message.id == "welcome"
    assert message.sender == "noreply@example.test"
    assert message.text
    assert "482913" in message.text


async def test_get_on_a_missing_body_block_raises_schema_drift():
    broken = load("readmail.html").replace('<div class="mailinhtml">', '<div class="gone">')
    http = fake({**default_routes(), "readmail.html?mid=welcome": broken})
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    with pytest.raises(SchemaDrift):
        await provider.get(address, "welcome")


async def test_generate_on_a_page_without_the_address_input_raises_schema_drift():
    http = fake({"GET https://10minutemail.net/?": "<html><body>blocked</body></html>"})
    with pytest.raises(SchemaDrift):
        await TenMinuteMailNet(http).generate()


async def test_caps_describe_the_backend():
    caps = TenMinuteMailNet.caps
    assert caps.sites == ("10minutemail.net",)
    assert "zefvo.com" in caps.domains
    assert Kind.OWN_DOMAIN in caps.kind
    assert caps.fresh_session is True
    assert caps.needs_state is True
    assert caps.inbox_privacy is InboxPrivacy.PRIVATE


def test_cfemail_obfuscated_senders_are_decoded():
    # Cloudflare email-protection cloaks addresses on the readmail page; the
    # fixtures were neutralized, so pin the algorithm on the recorded blob.
    from cowbird_tenminutemailnet import _decode_cfemail

    assert _decode_cfemail("aec0c183dccbdec2d7ee9f9ec3c7c0dbdacbc3cfc7c280c0cbda") == (
        "no-reply@10minutemail.net"
    )


async def test_a_blocked_front_door_raises_provider_down_not_message_gone():
    # ProviderDown is reroutable and counts against health; MessageGone would
    # leave a dead front door looking healthy. 4xx, not 5xx: FakeTransport
    # raises ProviderDown itself for >=500, which would make this test pass
    # regardless of the adapter's own status branch.
    from cowbird.errors import ProviderDown
    from cowbird.testing import Reply

    http = fake({"GET https://10minutemail.net/?": Reply(status=403, payload="blocked")})
    with pytest.raises(ProviderDown):
        await TenMinuteMailNet(http).generate()


async def test_rendered_table_with_unparsable_rows_raises_schema_drift():
    # Row markup changed: silently returning [] would leave a caller polling
    # for a code forever with no error.
    changed = (
        '<table id="maillist"><tr><th>From</th></tr>'
        '<tr><td><a class="row-link" href="readmail.html?mid=welcome">s@x.test</a></td></tr>'
        "</table>"
    )
    http = fake({**default_routes(), "mailbox.ajax.php": changed})
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    with pytest.raises(SchemaDrift, match="parsable rows"):
        await provider.list(address)


async def test_non_404_4xx_on_the_read_page_raises_provider_down():
    from cowbird.errors import ProviderDown
    from cowbird.testing import Reply

    http = fake(
        {**default_routes(), "readmail.html?mid=welcome": Reply(status=403, payload="denied")}
    )
    provider = TenMinuteMailNet(http)
    address = await provider.generate()
    with pytest.raises(ProviderDown):
        await provider.get(address, "welcome")


async def test_a_requested_domain_the_provider_does_not_serve_is_refused():
    from cowbird.errors import NotSupported
    from cowbird.provider import GenerateOptions

    http = fake(default_routes())
    with pytest.raises(NotSupported):
        await TenMinuteMailNet(http).generate(GenerateOptions(domain="not-a-real-domain.invalid"))


async def test_a_dead_session_after_the_ttl_is_address_expired():
    from datetime import UTC, datetime, timedelta

    from cowbird.errors import AddressExpired
    from cowbird_tenminutemailnet import TenMinuteMailNet

    provider = TenMinuteMailNet(fake({**default_routes(), "mailbox.ajax.php": "<p>login</p>"}))
    address = await TenMinuteMailNet(fake(default_routes())).generate()
    old = Address(address.value, address.provider, datetime.now(UTC) - timedelta(minutes=1),
                  address.state)
    with pytest.raises(AddressExpired):
        await provider.list(old)


async def test_a_non_table_answer_inside_the_ttl_is_still_drift():
    from cowbird_tenminutemailnet import TenMinuteMailNet

    address = await TenMinuteMailNet(fake(default_routes())).generate()
    provider = TenMinuteMailNet(fake({**default_routes(), "mailbox.ajax.php": "<p>login</p>"}))
    with pytest.raises(SchemaDrift):
        await provider.list(address)


@pytest.mark.parametrize("state", [None, "not json", "{}"])
async def test_missing_or_malformed_state_is_not_supported(state):
    from cowbird.errors import NotSupported
    from cowbird_tenminutemailnet import TenMinuteMailNet

    with pytest.raises(NotSupported):
        await TenMinuteMailNet(fake(default_routes())).list(
            Address("user01@zefvo.com", "tenminutemailnet", state=state)
        )


def _real_maillist() -> str:
    # The site's own page as recorded, Cloudflare's <span data-cfemail> intact.
    page = load("home.html")
    start = page.index('<table id="maillist"')
    return page[start : page.index("</table>", start) + len("</table>")]


async def test_rows_from_the_real_markup_land_in_the_right_columns():
    from cowbird_tenminutemailnet import TenMinuteMailNet

    address = await TenMinuteMailNet(fake(default_routes())).generate()
    provider = TenMinuteMailNet(fake({**default_routes(), "mailbox.ajax.php": _real_maillist()}))
    [row] = await provider.list(address)
    assert row.subject == "Hi, Welcome to 10 Minute Mail"
    assert "@" in row.sender and "protected" not in row.sender


async def test_a_nested_body_is_kept_whole():
    from cowbird_tenminutemailnet import TenMinuteMailNet

    page = load("readmail.html")
    start = page.index('<div class="mailinhtml">')
    nested = '<div class="mailinhtml"><div><div>Hello</div></div><p>code 123456</p></div>'
    end = page.index("</div>", start) + len("</div>")
    routes = {**default_routes(), "readmail.html?mid=welcome": page[:start] + nested + page[end:]}
    address = await TenMinuteMailNet(fake(default_routes())).generate()
    message = await TenMinuteMailNet(fake(routes)).get(address, "welcome")
    assert "123456" in message.text
