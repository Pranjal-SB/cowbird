"""Unit tests for the tmail.link adapter, against fixtures recorded through a
real Transport (see the report for the recording recipes)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply
from cowbird_tmaillink import TMailLink

FIXTURES = Path(__file__).parent / "fixtures"

ADDRESS = Address("reader@tmail.link", "tmaillink")
KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TOKEN = "bBWMnI11DHde9igco96R1V2d5t0eRU0ky3bJqqn3ROxfg3kVcITagSERlvYmC1dL"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class StubTransport(FakeTransport):
    """FakeTransport plus cookie capture on send(), so tests can assert the
    csrftoken travelled with the POST."""


def fake_provider(**overrides):
    routes = {
        "GET https://tmail.link/": Reply(200, load("home.html"), cookies={"csrftoken": TOKEN}),
        "GET https://tmail.link/inbox/reader@tmail.link/": Reply(
            200, load("home.html"), cookies={"csrftoken": TOKEN}
        ),
        "POST https://tmail.link/inbox/reader@tmail.link/": load("list_one.json"),
        KEY: load("message.html"),
        f"{KEY}/1": load("message_body.html"),
        **overrides,
    }
    http = StubTransport("tmaillink", routes)
    return TMailLink(http), http


# --------------------------------------------------------------------------
# list(): POST format=json with the csrf token
# --------------------------------------------------------------------------

async def test_list_posts_the_json_format_with_a_csrf_token():
    provider, http = fake_provider()
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    await provider.list(address)
    method, url, kw = http.seen[-1]
    assert (method, url) == ("POST", "https://tmail.link/inbox/reader@tmail.link/")
    assert kw["data"]["format"] == "json"
    assert kw["data"]["csrfmiddlewaretoken"] == TOKEN


async def test_list_parses_the_rows():
    provider, _ = fake_provider()
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    rows = await provider.list(address)
    assert len(rows) == 1
    assert rows[0].id == KEY
    assert rows[0].sender == "Example Notifications <noreply@example.test>"
    assert rows[0].subject == "Your verification code is 482913."
    assert rows[0].received_at == datetime(2026, 11, 15, 12, 51, 33, tzinfo=UTC)


async def test_an_empty_inbox_is_an_empty_list():
    provider, _ = fake_provider(
        **{"POST https://tmail.link/inbox/reader@tmail.link/": load("list_empty.json")}
    )
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    assert await provider.list(address) == []


async def test_list_without_state_fetches_a_token_itself():
    # A caller resuming a PUBLIC inbox out of process without state still works:
    # the adapter opens the inbox page once, takes the csrftoken it is handed.
    provider, http = fake_provider()
    rows = await provider.list(ADDRESS)
    assert rows
    methods = [(m, u) for m, u, _ in http.seen]
    assert ("GET", "https://tmail.link/inbox/reader@tmail.link/") in methods
    assert ("POST", "https://tmail.link/inbox/reader@tmail.link/") in methods


async def test_a_list_answer_without_messages_is_drift():
    provider, _ = fake_provider(
        **{"POST https://tmail.link/inbox/reader@tmail.link/": {"nope": []}}
    )
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    with pytest.raises(SchemaDrift):
        await provider.list(address)


async def test_a_row_without_a_key_is_drift():
    provider, _ = fake_provider(
        **{
            "POST https://tmail.link/inbox/reader@tmail.link/": {
                "messages": [{"sender": "x", "subject": "y"}]
            }
        }
    )
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    with pytest.raises(SchemaDrift):
        await provider.list(address)


# --------------------------------------------------------------------------
# get(): the message page, then the body iframe
# --------------------------------------------------------------------------

async def test_get_reads_the_body_from_the_iframe():
    provider, http = fake_provider()
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    message = await provider.get(address, KEY)
    assert message.id == KEY
    assert message.sender == "Example Notifications <noreply@example.test>"
    assert message.subject == "Your verification code is 482913."
    assert "482913" in message.text
    assert message.html.strip().startswith("<base")
    assert "https://example.test" in message.links or not message.links
    methods = [(m, u) for m, u, _ in http.seen]
    assert ("GET", f"https://tmail.link/inbox/reader@tmail.link/{KEY}/1/") in methods


async def test_a_plain_text_message_reads_its_inline_pre():
    # No iframe for text/plain mail: the body renders in a <pre> on the page.
    provider, http = fake_provider(**{KEY: load("message_plain.html")})
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    message = await provider.get(address, KEY)
    assert "482913" in message.text
    assert "expires in 10 minutes" in message.text
    # no second fetch: the pre was the body
    assert not any(u.endswith(f"/{KEY}/1/") for _, u, _ in http.seen)


async def test_an_unknown_key_is_gone():
    provider, _ = fake_provider(**{KEY: Reply(404, load("unknown_key.html"))})
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    with pytest.raises(MessageGone):
        await provider.get(address, KEY)


async def test_a_message_page_without_an_iframe_is_drift():
    provider, _ = fake_provider(**{KEY: "<html><body>moved</body></html>"})
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    with pytest.raises(SchemaDrift):
        await provider.get(address, KEY)


# --------------------------------------------------------------------------
# generate(): scrape the random address, or keep a custom local part
# --------------------------------------------------------------------------

async def test_generate_scrapes_the_random_address_from_the_homepage():
    provider, http = fake_provider()
    address = await provider.generate()
    assert address.value == "francis.lum@tmail.link"
    assert address.provider == "tmaillink"
    methods = [(m, u) for m, u, _ in http.seen]
    assert ("GET", "https://tmail.link/inbox/francis.lum@tmail.link/") in methods


async def test_generate_packs_the_csrftoken_into_state():
    provider, _ = fake_provider()
    address = await provider.generate()
    assert json.loads(address.state)["csrftoken"]


async def test_a_chosen_local_is_kept():
    provider, _ = fake_provider()
    address = await provider.generate(GenerateOptions(local="qa.bot-1"))
    assert address.value == "qa.bot-1@tmail.link"


async def test_an_unserved_domain_is_refused():
    with pytest.raises(NotSupported):
        await fake_provider()[0].generate(GenerateOptions(domain="not-ours.test"))


async def test_only_the_own_domain_kind_is_served():
    with pytest.raises(NotSupported):
        await fake_provider()[0].generate(GenerateOptions(kind=Kind.EDU))


async def test_a_homepage_without_an_address_is_drift():
    provider, _ = fake_provider(**{"GET https://tmail.link/": "<html>no address</html>"})
    with pytest.raises(SchemaDrift):
        await provider.generate()


# --------------------------------------------------------------------------
# delete(): the message page's own form
# --------------------------------------------------------------------------

async def test_delete_posts_the_form_the_page_renders():
    provider, http = fake_provider()
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    await provider.delete(address, KEY)
    method, url, kw = http.seen[-1]
    assert (method, url) == ("POST", f"https://tmail.link/inbox/reader@tmail.link/{KEY}/")
    assert kw["data"] == {"csrfmiddlewaretoken": TOKEN, "delete": "1"}


# --------------------------------------------------------------------------
# capabilities
# --------------------------------------------------------------------------

def test_caps_declare_the_site():
    caps = TMailLink.caps
    assert caps.sites == ("tmail.link",)
    assert caps.domains == ("tmail.link",)
    assert caps.domain_count == 1
    assert caps.custom_local is True
    assert caps.self_hosted is True
    assert caps.push is False
    assert caps.delete is True
    assert caps.inbox_privacy.value == "public"
    assert Kind.OWN_DOMAIN in caps.kind


@pytest.mark.parametrize("status", [400, 403])
async def test_a_refused_message_page_is_down_not_drift(status):
    from cowbird.errors import ProviderDown

    provider, _ = fake_provider(**{KEY: Reply(status, "<html><h1>Forbidden</h1></html>")})
    with pytest.raises(ProviderDown):
        await provider.get(ADDRESS, KEY)


async def test_list_from_another_process_sends_the_csrf_cookie():
    # `cowbird wait` is a new process with an empty cookie jar. Django checks
    # the posted token against the csrftoken cookie, so without the cookie the
    # POST is a 403 page, which read as drift and quarantined the provider.
    provider, http = fake_provider()
    address = Address(ADDRESS.value, "tmaillink", state=json.dumps({"csrftoken": TOKEN}))
    await provider.list(address)
    method, url, kw = http.seen[-1]
    assert method == "POST"
    assert kw["cookies"] == {"csrftoken": TOKEN}
