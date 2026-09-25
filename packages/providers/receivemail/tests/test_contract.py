import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_receivemail import ReceiveMail

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "https://www.receivemail.org"
USER = f"GET {SITE}/user.php"
MAIL = f"GET {SITE}/mail.php"
# Recorded from a throwaway address of our own after one JoltMx test send.
OWN = "cb97375b75@getsomail.com"
MESSAGE_ID = "10120183"
ADDRESS = Address(OWN, "receivemail", state="fixture-session")


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    return {
        USER: Responses(
            Reply(200, "fixture.one@getsomail.com", cookies={"PHPSESSID": "fixture-session-1"}),
            Reply(200, "fixture.two@getsomail.com", cookies={"PHPSESSID": "fixture-session-2"}),
        ),
        MAIL: page("mail.html"),
        **overrides,
    }


def sent(http, url):
    return [kw for _, u, kw in http.seen if u == url]


class TestReceiveMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return ReceiveMail(FakeTransport("receivemail", routes()))


def test_it_asks_for_a_fresh_session_per_request():
    # The inbox is whatever user.php last bound to the PHP session, so on a
    # shared jar two addresses would read one inbox.
    assert ReceiveMail.caps.fresh_session is True


async def test_generate_asks_for_a_random_address_and_keeps_the_session():
    http = FakeTransport("receivemail", routes())
    address = await ReceiveMail(http).generate()
    assert address.value == "fixture.one@getsomail.com"
    assert address.state == "fixture-session-1"
    assert address.expires_at is None
    (user,) = sent(http, f"{SITE}/user.php")
    assert user["params"] == {"user": ""}


async def test_generate_takes_a_chosen_local_part_and_domain():
    answer = Reply(200, "hello@barooko.com", cookies={"PHPSESSID": "s"})
    http = FakeTransport("receivemail", routes(**{USER: answer}))
    address = await ReceiveMail(http).generate(GenerateOptions(local="Hello", domain="barooko.com"))
    assert address.value == "hello@barooko.com"
    (user,) = sent(http, f"{SITE}/user.php")
    assert user["params"] == {"user": "hello@barooko.com"}


async def test_a_domain_alone_gets_a_local_part_picked_for_it():
    # user.php ignores a bare "@domain" and hands out a random ofisher.net one.
    http = FakeTransport("receivemail", routes())
    provider = ReceiveMail(http)
    with pytest.raises(NotSupported):
        # The fixture answers getsomail.com, not the barooko.com asked for.
        await provider.generate(GenerateOptions(domain="barooko.com"))
    (user,) = sent(http, f"{SITE}/user.php")
    local, domain = user["params"]["user"].split("@")
    assert domain == "barooko.com"
    assert re.fullmatch(r"[0-9a-f]{10}", local)


async def test_a_domain_the_site_does_not_serve_is_refused():
    http = FakeTransport("receivemail", routes())
    with pytest.raises(NotSupported):
        await ReceiveMail(http).generate(GenerateOptions(domain="gmail.com"))
    assert http.seen == []


async def test_an_address_the_site_rewrote_is_refused():
    # It drops characters it does not like: "cb test@" comes back "cbtest@".
    answer = Reply(200, "cbtest@getsomail.com", cookies={"PHPSESSID": "s"})
    http = FakeTransport("receivemail", routes(**{USER: answer}))
    with pytest.raises(NotSupported):
        await ReceiveMail(http).generate(GenerateOptions(local="cb test", domain="getsomail.com"))


@pytest.mark.parametrize(
    "answer",
    [
        Reply(200, "fixture@getsomail.com"),
        Reply(200, "<html>maintenance</html>", cookies={"PHPSESSID": "s"}),
    ],
)
async def test_a_user_answer_without_an_address_and_session_is_drift(answer):
    http = FakeTransport("receivemail", routes(**{USER: answer}))
    with pytest.raises(SchemaDrift):
        await ReceiveMail(http).generate()


async def test_list_reads_the_rows_with_the_session_from_state():
    http = FakeTransport("receivemail", routes())
    rows = await ReceiveMail(http).list(ADDRESS)
    assert [kw["cookies"] for kw in sent(http, f"{SITE}/mail.php")] == [
        {"PHPSESSID": "fixture-session"}
    ]
    assert len(rows) == 1
    assert rows[0].id == MESSAGE_ID
    assert rows[0].subject == "JoltMx test email (ref 01a0d69eb352)"
    assert rows[0].sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    # The page's time is UTC: it matched the "Sent: ... UTC" in the body.
    assert rows[0].received_at == datetime(2026, 9, 25, 3, 31, 59, tzinfo=UTC)


async def test_an_empty_inbox_is_an_empty_list():
    http = FakeTransport("receivemail", routes(**{MAIL: page("mail_empty.html")}))
    assert await ReceiveMail(http).list(ADDRESS) == []


async def test_a_lost_session_is_bound_to_the_address_again():
    # PHP drops idle sessions; user.php binds whatever session id it is sent.
    http = FakeTransport(
        "receivemail",
        routes(**{MAIL: Responses(page("session_lost.html"), page("mail.html")), USER: OWN}),
    )
    rows = await ReceiveMail(http).list(ADDRESS)
    assert [r.id for r in rows] == [MESSAGE_ID]
    (user,) = sent(http, f"{SITE}/user.php")
    assert user["params"] == {"user": OWN}
    assert user["cookies"] == {"PHPSESSID": "fixture-session"}


async def test_a_session_that_will_not_bind_is_drift():
    http = FakeTransport("receivemail", routes(**{MAIL: page("session_lost.html"), USER: OWN}))
    with pytest.raises(SchemaDrift):
        await ReceiveMail(http).list(ADDRESS)


async def test_a_page_it_does_not_recognise_is_drift():
    http = FakeTransport("receivemail", routes(**{MAIL: "<html>maintenance</html>"}))
    with pytest.raises(SchemaDrift):
        await ReceiveMail(http).list(ADDRESS)


@pytest.mark.parametrize("bad", [None, ""])
async def test_list_without_the_session_is_refused(bad):
    provider = ReceiveMail(FakeTransport("receivemail", routes()))
    with pytest.raises(NotSupported):
        await provider.list(Address(OWN, "receivemail", state=bad))


async def test_get_reads_the_body_from_the_srcdoc():
    message = await ReceiveMail(FakeTransport("receivemail", routes())).get(ADDRESS, MESSAGE_ID)
    assert message.id == MESSAGE_ID
    assert message.subject == "JoltMx test email (ref 01a0d69eb352)"
    assert message.sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert message.received_at == datetime(2026, 9, 25, 3, 31, 59, tzinfo=UTC)
    assert "<h2" in message.html
    assert "everything is working as expected" in message.text
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)


async def test_a_message_the_page_no_longer_has_is_gone():
    with pytest.raises(MessageGone):
        await ReceiveMail(FakeTransport("receivemail", routes())).get(ADDRESS, "1")


async def test_a_message_without_an_iframe_body_still_reads_as_text():
    # Only HTML mail was seen live. A plain-text mail may render without a
    # srcdoc, and that must not quarantine the provider.
    plain = re.sub(
        r"<iframe srcdoc=\"[^\"]*\"[^>]*>(</iframe>)?",
        "<pre>Your code is 482913.</pre>",
        page("mail.html"),
    )
    message = await ReceiveMail(FakeTransport("receivemail", routes(**{MAIL: plain}))).get(
        ADDRESS, MESSAGE_ID
    )
    assert "Your code is 482913." in message.text
    assert "Delete" not in message.text
