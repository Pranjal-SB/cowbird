import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_mtempmail import MTempMail

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://mtempmail.com"
MINT = f"POST {BASE}/get_messages"
SESSION = "free_edu_com_temporary_mails_session"
INBOX_1 = "fixture-inbox-one"
INBOX_2 = "fixture-inbox-two"
INBOX_CHANGED = "fixture-inbox-changed"
# A listing is the mint request plus the inbox cookie. Routed on body and
# cookie together so it outranks MINT but not /change, which sends the
# cookie as well.
LISTING = '{"_token": "", "captcha": ""} email='


def raw(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def load(name):
    return json.loads(raw(name))


NEW_1 = load("messages_new_1.json")
NEW_2 = load("messages_new_2.json")
# The recorded mail, served for the first generated inbox.
WITH_MAIL = {**NEW_1, "messages": load("messages_one.json")["messages"]}
MAIL_ID = WITH_MAIL["messages"][0]["id"]


def routes(**overrides):
    return {
        MINT: Responses(
            Reply(200, raw("messages_new_1.json"), cookies={"email": INBOX_1}),
            Reply(200, raw("messages_new_2.json"), cookies={"email": INBOX_2}),
        ),
        f"{LISTING}{INBOX_1}": json.dumps(WITH_MAIL),
        f"{LISTING}{INBOX_2}": raw("messages_new_2.json"),
        f"GET {BASE}/?": Reply(200, raw("home.html"), cookies={SESSION: "fixture-session"}),
        f"POST {BASE}/change": Reply(200, raw("change.json"), cookies={"email": INBOX_CHANGED}),
        **overrides,
    }


def inbox(address_value, cookie):
    return Address(address_value, "mtempmail", state=json.dumps({"email": cookie}))


FIRST = inbox(NEW_1["mailbox"], INBOX_1)
SECOND = inbox(NEW_2["mailbox"], INBOX_2)


def sent(http, method, path):
    return [kw for m, u, kw in http.seen if m == method and u == f"{BASE}{path}"]


class TestMTempMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return MTempMail(FakeTransport("mtempmail", routes()))


def test_it_runs_each_request_on_a_fresh_session():
    # On a shared jar a second generate() would send the first inbox's cookie
    # and get the first address back.
    assert MTempMail.caps.fresh_session is True
    assert MTempMail.caps.needs_state is True


def test_it_serves_the_edu_pl_domain_as_edu():
    assert MTempMail.caps.serves(Kind.EDU)
    assert "zub.edu.pl" in MTempMail.caps.domains


async def test_generate_keeps_the_inbox_cookie_in_state():
    http = FakeTransport("mtempmail", routes())
    address = await MTempMail(http).generate()
    assert address.value == NEW_1["mailbox"]
    assert json.loads(address.state) == {"email": INBOX_1}
    (mint,) = sent(http, "POST", "/get_messages")
    assert not mint.get("cookies")


async def test_list_and_get_replay_the_inbox_cookie():
    http = FakeTransport("mtempmail", routes())
    provider = MTempMail(http)
    await provider.list(FIRST)
    await provider.get(FIRST, MAIL_ID)
    replays = sent(http, "POST", "/get_messages")
    assert [kw["cookies"] for kw in replays] == [{"email": INBOX_1}] * 2


async def test_an_empty_inbox_is_an_empty_list():
    assert await MTempMail(FakeTransport("mtempmail", routes())).list(SECOND) == []


async def test_list_reads_the_row():
    (row,) = await MTempMail(FakeTransport("mtempmail", routes())).list(FIRST)
    assert row.id == MAIL_ID
    assert row.sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert row.subject == "JoltMx test email (ref 01a0d79f4181)"
    # receivedAt is the site's local time, two hours ahead of UTC.
    assert row.received_at == datetime(2026, 9, 25, 8, 12, 13, tzinfo=UTC)


async def test_get_reads_the_body_the_listing_carries():
    message = await MTempMail(FakeTransport("mtempmail", routes())).get(FIRST, MAIL_ID)
    assert message.id == MAIL_ID
    assert message.subject == "JoltMx test email (ref 01a0d79f4181)"
    assert message.html.startswith("<!DOCTYPE html>")
    assert "This is a test email from JoltMx" in message.text
    assert "https://joltmx.com" in message.links


async def test_a_plain_text_message_is_read_as_text():
    # Synthetic: the recorded row with the site's `html: false` flag.
    row = {**WITH_MAIL["messages"][0], "html": False, "content": "code 123456"}
    http = FakeTransport(
        "mtempmail", routes(**{f"{LISTING}{INBOX_1}": {**WITH_MAIL, "messages": [row]}})
    )
    message = await MTempMail(http).get(FIRST, MAIL_ID)
    assert message.text == "code 123456"
    assert message.html == ""


async def test_a_message_the_listing_no_longer_has_is_gone():
    with pytest.raises(MessageGone):
        await MTempMail(FakeTransport("mtempmail", routes())).get(FIRST, "no-such-id")


async def test_a_cookie_the_site_answers_with_another_inbox_is_expired():
    # A cookie that no longer names a live inbox gets a freshly minted one.
    stale = inbox("gone@zub.edu.pl", INBOX_2)
    with pytest.raises(AddressExpired):
        await MTempMail(FakeTransport("mtempmail", routes())).list(stale)


@pytest.mark.parametrize(
    "answer",
    [
        '{"status": false}',
        json.dumps({**NEW_1, "messages": "none"}),
        json.dumps({**NEW_1, "messages": [{"subject": "no id"}]}),
        json.dumps({**NEW_1, "messages": [{"id": "1", "content": None}]}),
        "<html>maintenance</html>",
    ],
)
async def test_a_listing_it_does_not_recognise_is_drift(answer):
    http = FakeTransport("mtempmail", routes(**{f"{LISTING}{INBOX_1}": answer}))
    with pytest.raises(SchemaDrift):
        await MTempMail(http).list(FIRST)


async def test_a_mint_without_the_inbox_cookie_is_drift():
    http = FakeTransport("mtempmail", routes(**{MINT: raw("messages_new_1.json")}))
    with pytest.raises(SchemaDrift):
        await MTempMail(http).generate()


@pytest.mark.parametrize("bad", [None, "", "not json", '{"cookie": "x"}'])
async def test_list_without_usable_state_is_refused(bad):
    provider = MTempMail(FakeTransport("mtempmail", routes()))
    with pytest.raises(NotSupported):
        await provider.list(Address(FIRST.value, "mtempmail", state=bad))


async def test_a_chosen_local_part_and_domain_go_through_change():
    http = FakeTransport("mtempmail", routes())
    address = await MTempMail(http).generate(
        GenerateOptions(local="cowbirdprobe9051", domain="zub.edu.pl")
    )
    assert address.value == "cowbirdprobe9051@zub.edu.pl"
    assert json.loads(address.state) == {"email": INBOX_CHANGED}
    (change,) = sent(http, "POST", "/change")
    assert change["json"] == {
        "_token": "0" * 40,
        "name": "cowbirdprobe9051",
        "domain": "zub.edu.pl",
    }
    assert change["headers"]["X-CSRF-TOKEN"] == "0" * 40
    assert change["cookies"] == {SESSION: "fixture-session", "email": INBOX_1}


async def test_a_domain_the_mint_already_landed_on_needs_no_change():
    http = FakeTransport("mtempmail", routes())
    domain = NEW_1["mailbox"].split("@")[1]
    address = await MTempMail(http).generate(GenerateOptions(domain=domain))
    assert address.value == NEW_1["mailbox"]
    assert sent(http, "POST", "/change") == []


async def test_the_edu_kind_moves_the_minted_name_to_the_edu_domain():
    http = FakeTransport("mtempmail", routes())
    await MTempMail(http).generate(GenerateOptions(kind=Kind.EDU))
    (change,) = sent(http, "POST", "/change")
    assert change["json"]["name"] == NEW_1["mailbox"].split("@")[0]
    assert change["json"]["domain"] == "zub.edu.pl"


@pytest.mark.parametrize(
    "opts",
    [GenerateOptions(domain="gmail.com"), GenerateOptions(kind=Kind.GMAIL_ALIAS)],
)
async def test_what_it_does_not_serve_is_refused_before_any_request(opts):
    http = FakeTransport("mtempmail", routes())
    with pytest.raises(NotSupported):
        await MTempMail(http).generate(opts)
    assert http.seen == []


async def test_a_name_the_site_rejects_is_not_supported():
    http = FakeTransport(
        "mtempmail",
        routes(**{f"POST {BASE}/change": Reply(422, raw("change_bad_domain.json"))}),
    )
    with pytest.raises(NotSupported):
        await MTempMail(http).generate(GenerateOptions(local="x1", domain="zub.edu.pl"))


@pytest.mark.parametrize("answer", ["return", '{"message": "CSRF token mismatch."}'])
async def test_a_change_answer_it_does_not_recognise_is_drift(answer):
    # "return" is what /change answers a session holding no inbox.
    http = FakeTransport("mtempmail", routes(**{f"POST {BASE}/change": answer}))
    with pytest.raises(SchemaDrift):
        await MTempMail(http).generate(GenerateOptions(local="abc", domain="zub.edu.pl"))


async def test_a_home_page_without_a_csrf_token_is_drift():
    http = FakeTransport("mtempmail", routes(**{f"GET {BASE}/?": "<html></html>"}))
    with pytest.raises(SchemaDrift):
        await MTempMail(http).generate(GenerateOptions(local="abc"))
