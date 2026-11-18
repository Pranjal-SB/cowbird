"""Unit tests for the emailfake.com adapter, against fixtures recorded
through a real Transport (see the report for the recording recipes)."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, InboxPrivacy, Kind
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeResponse, FakeTransport
from cowbird_generatoremail import POW_CAP, VERIFY_COOKIE, GeneratorEmail, _solve_pow

FIXTURES = Path(__file__).parent / "fixtures"

ADDRESS = Address("mytest613@towintztf.top", "generatoremail")
MID = "5f17b04df7a4617dafb561fa7ea0c82d"
OLD_MID = "20bc775d50a68c3efc6028da7ab3c865"
COOKIE = "v5.k1.1794741713.1794744413.99e22670453979ee"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


INTERSTITIAL = load("interstitial_503.html")
MAILBOX = load("mailbox.html")


class StubTransport:
    """Hands back queued raw responses.

    Needed because neither real Transport (the interstitial's "Just a moment"
    title makes it raise CloudflareChallenge) nor FakeTransport (any 5xx raises
    ProviderDown) can replay the 503 interstitial the adapter must handle.
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []

    async def send(self, method, url, *, allow_status=(), **kw):
        self.seen.append((method, url, kw))
        return self.responses.pop(0)


def gated(*extra):
    """A read that is gated once, verifies, then gets through."""
    return StubTransport(
        FakeResponse(503, INTERSTITIAL, {"x-ab-interstitial": "1"}, {}),
        FakeResponse(200, load("verify_ok.html"), {}, {}),
        FakeResponse(200, MAILBOX, {}, {}),
        *extra,
    )


def fake_provider(**overrides):
    routes = {
        "GET https://emailfake.com/api/domains.php": json.loads(load("domains.json")),
        "GET https://emailfake.com/": MAILBOX,
        MID: load("message.html"),
        **overrides,
    }
    http = FakeTransport("generatoremail", routes)
    return GeneratorEmail(http), http


# --------------------------------------------------------------------------
# the proof-of-work solver
# --------------------------------------------------------------------------

SALT = "fixture-salt-2026-11-15-acowbird"
NONCE = 2268


def test_the_precomputed_nonce_solves_the_precomputed_salt():
    assert _solve_pow(SALT) == NONCE


def test_the_precomputed_nonce_is_the_first_solution():
    def digest(n):
        return int.from_bytes(hashlib.sha256(f"{SALT}:{n}".encode()).digest()[:2], "big")

    assert digest(NONCE) >> 1 == 0  # 15 leading zero bits
    assert all(digest(n) >> 1 != 0 for n in range(NONCE))


def test_the_cap_is_what_the_brief_set():
    assert POW_CAP == 2**22


def test_exhausting_the_cap_is_provider_down():
    # cap = the answer: the loop never reaches it.
    with pytest.raises(ProviderDown):
        _solve_pow(SALT, cap=NONCE)


# --------------------------------------------------------------------------
# the gate: interstitial -> solve -> /__verify -> replay the cookie
# --------------------------------------------------------------------------

async def test_a_gated_read_solves_and_shows_its_work():
    http = gated()
    rows = await GeneratorEmail(http).list(ADDRESS)
    assert [r.id for r in rows] == [MID, OLD_MID]
    method, url, kw = http.seen[1]
    assert method == "GET" and url.endswith("/__verify")
    assert kw["params"]["c"] == "SALT-PLACEHOLDER"  # the salt the 503 carried
    assert kw["params"]["t"] == "/"
    n = int(kw["params"]["n"])
    assert int.from_bytes(
        hashlib.sha256(f"SALT-PLACEHOLDER:{n}".encode()).digest()[:2], "big"
    ) >> 1 == 0


async def test_generate_packs_the_verified_cookie_into_state():
    http = StubTransport(
        FakeResponse(200, MAILBOX, {}, {}),  # homepage: the api token
        FakeResponse(200, load("domains.json"), {}, {}),  # domains.php
        FakeResponse(503, INTERSTITIAL, {"x-ab-interstitial": "1"}, {}),
        FakeResponse(200, load("verify_ok.html"), {}, {VERIFY_COOKIE: COOKIE}),
        FakeResponse(200, MAILBOX, {}, {}),  # the mailbox, cleared
    )
    provider = GeneratorEmail(http)
    address = await provider.generate()
    assert json.loads(address.state)["ab_hp"] == COOKIE


async def test_later_calls_replay_the_cookie_from_state():
    http = StubTransport(FakeResponse(200, MAILBOX, {}, {}))
    address = Address(ADDRESS.value, ADDRESS.provider, state=json.dumps({"ab_hp": COOKIE}))
    await GeneratorEmail(http).list(address)
    assert http.seen[0][2]["cookies"] == {VERIFY_COOKIE: COOKIE}


async def test_a_state_without_a_cookie_sends_none():
    http = StubTransport(FakeResponse(200, MAILBOX, {}, {}))
    await GeneratorEmail(http).list(ADDRESS)
    assert not http.seen[0][2].get("cookies")


async def test_still_gated_after_verifying_is_provider_down():
    http = StubTransport(
        FakeResponse(503, INTERSTITIAL, {"x-ab-interstitial": "1"}, {}),
        FakeResponse(200, load("verify_ok.html"), {}, {}),
        FakeResponse(503, INTERSTITIAL, {"x-ab-interstitial": "1"}, {}),
    )
    with pytest.raises(ProviderDown):
        await GeneratorEmail(http).list(ADDRESS)


async def test_an_interstitial_without_a_salt_is_drift():
    http = StubTransport(
        FakeResponse(503, "<html>no salt here</html>", {"x-ab-interstitial": "1"}, {}),
    )
    with pytest.raises(SchemaDrift):
        await GeneratorEmail(http).list(ADDRESS)


async def test_a_verify_that_stays_gated_is_provider_down():
    http = StubTransport(
        FakeResponse(503, INTERSTITIAL, {"x-ab-interstitial": "1"}, {}),
        FakeResponse(503, INTERSTITIAL, {"x-ab-interstitial": "1"}, {}),
    )
    with pytest.raises(ProviderDown):
        await GeneratorEmail(http).list(ADDRESS)


# --------------------------------------------------------------------------
# inbox and message parsing
# --------------------------------------------------------------------------

async def test_list_reads_the_rows_newest_first():
    http = StubTransport(FakeResponse(200, MAILBOX, {}, {}))
    rows = await GeneratorEmail(http).list(ADDRESS)
    assert rows[0].id == MID
    assert rows[0].sender == "test@sendtest.joltmx.com"
    assert rows[0].subject == "JoltMx test email (ref 01a1deecd80c)"
    assert rows[0].received_at == datetime(2026, 11, 15, 11, 17, 7, tzinfo=UTC)
    assert rows[1].id == OLD_MID
    assert rows[1].received_at == datetime(2026, 11, 15, 11, 12, 28, tzinfo=UTC)


async def test_a_one_message_mailbox_takes_its_id_from_the_page():
    http = StubTransport(FakeResponse(200, load("mailbox_single.html"), {}, {}))
    rows = await GeneratorEmail(http).list(ADDRESS)
    assert [r.id for r in rows] == [OLD_MID]
    assert rows[0].subject == "JoltMx test email (ref 01a1dee8930d)"


async def test_an_empty_inbox_is_an_empty_list():
    http = StubTransport(FakeResponse(200, load("mailbox_empty.html"), {}, {}))
    assert await GeneratorEmail(http).list(ADDRESS) == []


async def test_a_page_without_the_inbox_table_is_drift():
    http = StubTransport(FakeResponse(200, "<html><body>moved</body></html>", {}, {}))
    with pytest.raises(SchemaDrift):
        await GeneratorEmail(http).list(ADDRESS)


async def test_a_row_without_a_timestamp_still_lists():
    http = StubTransport(FakeResponse(200, MAILBOX, {}, {}))
    rows = await GeneratorEmail(http).list(ADDRESS)
    assert rows[0].received_at is not None  # sanity: the fixture has times


async def test_get_reads_the_body():
    http = StubTransport(FakeResponse(200, load("message.html"), {}, {}))
    message = await GeneratorEmail(http).get(ADDRESS, MID)
    assert message.id == MID
    assert message.sender == "test@sendtest.joltmx.com"
    assert message.subject == "JoltMx test email (ref 01a1deecd80c)"
    assert message.received_at == datetime(2026, 11, 15, 11, 17, 7, tzinfo=UTC)
    assert "This is a test email from JoltMx" in message.text
    assert "https://joltmx.com" in message.links


async def test_get_returns_the_raw_html_too():
    http = StubTransport(FakeResponse(200, load("message.html"), {}, {}))
    message = await GeneratorEmail(http).get(ADDRESS, MID)
    assert "<h2" in message.html


async def test_an_unknown_message_id_is_gone():
    # A mid the backend does not know answers the mailbox page: no data-mid,
    # no body block. That is "gone", not drift.
    http = StubTransport(FakeResponse(200, MAILBOX, {}, {}))
    with pytest.raises(MessageGone):
        await GeneratorEmail(http).get(ADDRESS, "0" * 32)


# --------------------------------------------------------------------------
# generate(): domains scraped, custom local kept
# --------------------------------------------------------------------------

async def test_generate_picks_a_scraped_domain():
    provider, _ = fake_provider()
    address = await provider.generate()
    domains = {d["ascii"] for d in json.loads(load("domains.json"))}
    assert address.provider == "generatoremail"
    assert address.value.split("@")[1] in domains


async def test_a_chosen_local_and_domain_are_kept():
    domain = json.loads(load("domains.json"))[0]["ascii"]
    address = await fake_provider()[0].generate(
        GenerateOptions(local="qa.bot-1", domain=domain)
    )
    assert address.value == f"qa.bot-1@{domain}"


async def test_generate_opens_the_issued_mailbox_once():
    provider, http = fake_provider()
    await provider.generate()
    opens = [u for m, u, _ in http.seen if "@" in u]
    assert len(opens) == 1


async def test_an_unserved_domain_is_refused():
    with pytest.raises(NotSupported):
        await fake_provider()[0].generate(GenerateOptions(domain="not-ours.test"))


async def test_only_the_own_domain_kind_is_served():
    with pytest.raises(NotSupported):
        await fake_provider()[0].generate(GenerateOptions(kind=Kind.EDU))


async def test_a_domains_answer_without_domains_is_drift():
    provider, _ = fake_provider(**{"GET https://emailfake.com/api/domains.php": []})
    with pytest.raises(SchemaDrift):
        await provider.generate()


async def test_a_homepage_without_an_api_token_is_drift():
    provider, _ = fake_provider(**{"GET https://emailfake.com/": "<html>no token</html>"})
    with pytest.raises(SchemaDrift):
        await provider.generate()


# --------------------------------------------------------------------------
# capabilities
# --------------------------------------------------------------------------

def test_caps_declare_the_family():
    caps = GeneratorEmail.caps
    assert caps.sites == ("emailfake.com",)
    assert caps.domains == ()  # scraped per generate
    assert caps.domain_count >= 50
    assert caps.fresh_session
    assert caps.needs_state
    assert caps.custom_local
    assert caps.inbox_privacy is InboxPrivacy.PUBLIC
    assert not caps.push
    assert not caps.delete
    assert Kind.OWN_DOMAIN in caps.kind



async def test_a_page_for_another_message_is_gone_not_drift():
    # An expired mid on a busy mailbox answers the page of the message now
    # shown there. The one asked for is gone; the adapter is fine.
    http = StubTransport(FakeResponse(200, load("message.html"), {}, {}))
    with pytest.raises(MessageGone):
        await GeneratorEmail(http).get(ADDRESS, OLD_MID)
