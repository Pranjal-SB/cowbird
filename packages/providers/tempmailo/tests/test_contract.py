import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, RateLimited, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_tempmailo import TempMailo

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://tempmailo.com"
COOKIE = {".AspNetCore.Antiforgery.dXyz_uFU2og": "fixture-antiforgery-cookie"}
MESSAGE_ID = "6ab62f1259324493fbc1ec8e"
FULL = Address("lypibabi@forexzig.com", "tempmailo")
EMPTY = Address("cukiluzu@denipl.com", "tempmailo")
# The listing route for FULL; overrides use it so they outrank the shorter keys.
LIST = f'POST {BASE}/? {{"mail": "{FULL.value}"}}'


def raw(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    # FakeTransport target: "METHOD url?query jsonbody cookies headers".
    return {
        f"GET {BASE}/?": Reply(200, raw("home.html"), cookies=COOKIE),
        f"GET {BASE}/changemail": Responses(FULL.value, EMPTY.value),
        LIST: raw("messages.json"),
        f'POST {BASE}/? {{"mail": "{EMPTY.value}"}}': "[]",
        **overrides,
    }


def provider(**overrides):
    return TempMailo(FakeTransport("tempmailo", routes(**overrides)))


def calls(http, method, path):
    return [kw for m, u, kw in http.seen if m == method and u.startswith(f"{BASE}{path}")]


class TestTempMailoContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return provider()


def test_the_inbox_is_keyed_by_the_address_alone():
    # Any antiforgery pair reads any address, so there is no state to carry.
    assert TempMailo.caps.needs_state is False
    assert TempMailo.caps.fresh_session is False


async def test_generate_replays_the_page_token_and_its_cookie():
    http = FakeTransport("tempmailo", routes())
    address = await TempMailo(http).generate()
    assert address.value == FULL.value
    (change,) = calls(http, "GET", "/changemail")
    assert change["headers"]["RequestVerificationToken"] == "fixture-antiforgery-token"
    assert change["cookies"] == COOKIE


async def test_list_replays_the_page_token_and_its_cookie():
    http = FakeTransport("tempmailo", routes())
    await TempMailo(http).list(FULL)
    (post,) = calls(http, "POST", "/")
    assert post["json"] == {"mail": FULL.value}
    assert post["headers"]["RequestVerificationToken"] == "fixture-antiforgery-token"
    assert post["cookies"] == COOKIE


@pytest.mark.parametrize("opts", [GenerateOptions(local="me"), GenerateOptions(domain="fxzig.com")])
async def test_a_chosen_address_is_refused(opts):
    # The client's ?tmail= is ignored upstream.
    with pytest.raises(NotSupported):
        await provider().generate(opts)


async def test_the_rate_limit_answer_is_rate_limited():
    # Upstream says 400 "Rate limit exceeded!" after ~20 addresses per IP.
    with pytest.raises(RateLimited):
        await provider(**{f"GET {BASE}/changemail": Reply(400, "Rate limit exceeded!")}).generate()


@pytest.mark.parametrize("answer", ["", "<html>oops</html>", "not an address", "a@b@c"])
async def test_a_generate_answer_that_is_not_an_address_is_drift(answer):
    with pytest.raises(SchemaDrift):
        await provider(**{f"GET {BASE}/changemail": answer}).generate()


async def test_a_page_without_a_token_is_drift():
    with pytest.raises(SchemaDrift):
        await provider(**{f"GET {BASE}/?": Reply(200, "<html></html>", cookies=COOKIE)}).generate()


async def test_a_page_that_sets_no_new_cookie_still_reads_the_inbox():
    # The transport keeps one cookie jar per provider, and the site sets the
    # antiforgery cookie only once: from the second call on, the home page
    # answers without Set-Cookie and the jar supplies it.
    rows = await provider(**{f"GET {BASE}/?": raw("home.html")}).list(FULL)
    assert rows


async def test_a_refused_token_is_drift():
    # A token without its cookie gets a bare 400.
    with pytest.raises(SchemaDrift):
        await provider(**{LIST: Reply(400, "")}).list(FULL)


async def test_list_reads_the_rows():
    (row,) = await provider().list(FULL)
    assert row.id == MESSAGE_ID
    assert row.sender == '"JoltMx Delivery Test" <test@sendtest.joltmx.com>'
    assert row.subject == "JoltMx test email (ref 01a0d7a7d9a6)"
    assert row.received_at == datetime(2026, 9, 25, 8, 21, 36, 798000, tzinfo=UTC)


async def test_an_empty_inbox_is_an_empty_list():
    assert await provider().list(EMPTY) == []


@pytest.mark.parametrize(
    "answer",
    [
        "{}",
        "<html>error</html>",
        '[{"subject": "no id"}]',
        '["row"]',
        '[{"id": "1", "date": "soon"}]',
    ],
)
async def test_a_listing_it_does_not_recognise_is_drift(answer):
    with pytest.raises(SchemaDrift):
        await provider(**{LIST: answer}).list(FULL)


async def test_get_takes_the_body_from_the_row():
    message = await provider().get(FULL, MESSAGE_ID)
    assert message.id == MESSAGE_ID
    assert message.text.startswith("This is a test email from JoltMx.")
    assert "<h2" in message.html
    assert "https://joltmx.com" in message.links
    assert message.received_at == datetime(2026, 9, 25, 8, 21, 36, 798000, tzinfo=UTC)


async def test_get_falls_back_to_the_html_for_text():
    row = {"id": "1", "from": "a@x.test", "subject": "s", "html": "<p>hi</p>", "text": None}
    message = await provider(**{LIST: json.dumps([row])}).get(FULL, "1")
    assert message.text == "hi"


async def test_a_message_no_longer_listed_is_gone():
    with pytest.raises(MessageGone):
        await provider().get(EMPTY, MESSAGE_ID)


async def test_a_row_with_a_body_that_is_not_text_is_drift():
    row = {"id": "1", "html": {"x": 1}, "text": None}
    with pytest.raises(SchemaDrift):
        await provider(**{LIST: json.dumps([row])}).get(FULL, "1")
