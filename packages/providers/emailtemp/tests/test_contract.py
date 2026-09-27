import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_emailtemp import EmailTemp

FIXTURES = Path(__file__).parent / "fixtures"
ET = "https://emailtemp.org"
EG = "https://www.emailgenerator.org"
# host -> (base URL, Laravel session cookie, fixture prefix, first listing's fixture)
HOSTS = {
    "emailtemp.org": (ET, "emailtemp_session", "emailtemp", "emailtemp_messages_1.json"),
    "emailgenerator.org": (
        EG,
        "emailgenerator_session",
        "emailgenerator",
        "emailgenerator_messages_1_synthetic.json",
    ),
}


def raw(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def listing(base, session, tag, inbox):
    # FakeTransport target: "POST <url>?<query> <json> <cookies> <headers>".
    return f"POST {base}/messages?  {session}={tag}-session&email={tag}-{inbox}"


def host_routes(site):
    base, session, tag, first = HOSTS[site]
    return {
        f"GET {base}/en": Reply(200, raw(f"{tag}_home.html"), cookies={session: f"{tag}-session"}),
        # generate(): the session cookie alone, so the site mints a mailbox.
        f"POST {base}/messages": Responses(
            Reply(200, raw(f"{tag}_messages_new_1.json"), cookies={"email": f"{tag}-inbox-1"}),
            Reply(200, raw(f"{tag}_messages_new_2.json"), cookies={"email": f"{tag}-inbox-2"}),
        ),
        listing(base, session, tag, "inbox-1"): raw(first),
        listing(base, session, tag, "inbox-2"): raw(f"{tag}_messages_new_2.json"),
        f"POST {base}/create": Reply(302, "", cookies={"email": f"{tag}-named"}),
    }


def routes(**overrides):
    merged = {}
    for site in HOSTS:
        merged.update(host_routes(site))
    return {**merged, **overrides}


def state(site, cookie):
    return json.dumps({"site": site, "cookie": cookie}, separators=(",", ":"))


EMAILTEMP = Address(
    "cbtb77b22c1@tormails.com", "emailtemp", state=state("emailtemp.org", "emailtemp-inbox-1")
)
EMPTY = Address(
    "jtxpyxa689@tormails.com", "emailtemp", state=state("emailtemp.org", "emailtemp-inbox-2")
)
GENERATOR = Address(
    "ljegjla768@grnail.cam",
    "emailtemp",
    state=state("emailgenerator.org", "emailgenerator-inbox-1"),
)


def sent(http, method, url):
    return [kw for m, u, kw in http.seen if m == method and u == url]


def hosts_hit(http):
    return {u.split("/")[2] for _, u, _ in http.seen}


class TestEmailTempContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return EmailTemp(FakeTransport("emailtemp", routes()))


def test_it_asks_for_a_fresh_session_per_request():
    # The mailbox is the `email` cookie; on a shared jar a second generate()
    # would get the first mailbox back.
    assert EmailTemp.caps.fresh_session is True
    assert EmailTemp.caps.needs_state is True


def test_it_covers_both_hosts_of_the_script():
    assert set(EmailTemp.caps.sites) == set(HOSTS)
    assert EmailTemp.caps.custom_local is True


@pytest.mark.parametrize(
    ("domain", "host"),
    [("tormails.com", "emailtemp.org"), ("grnail.cam", "www.emailgenerator.org")],
)
async def test_generate_goes_only_to_the_host_serving_the_domain(domain, host):
    http = FakeTransport("emailtemp", routes())
    address = await EmailTemp(http).generate(GenerateOptions(domain=domain))
    assert address.value.endswith(f"@{domain}")
    assert hosts_hit(http) == {host}


async def test_generate_replays_the_session_and_csrf_into_messages():
    http = FakeTransport("emailtemp", routes())
    address = await EmailTemp(http).generate(GenerateOptions(domain="tormails.com"))
    (post,) = sent(http, "POST", f"{ET}/messages")
    assert post["data"] == {"_token": "fixture-csrf-token", "captcha": ""}
    assert post["cookies"] == {"emailtemp_session": "emailtemp-session"}
    assert post["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert address.value == "cbtb77b22c1@tormails.com"
    assert json.loads(address.state) == {"site": "emailtemp.org", "cookie": "emailtemp-inbox-1"}


async def test_a_domain_the_random_mailbox_missed_is_claimed_through_create():
    # emailgenerator hands out any of its six domains; the first mailbox here
    # is on grnail.cam, so asking for pluniversity.edu.pl goes through /create.
    http = FakeTransport("emailtemp", routes())
    address = await EmailTemp(http).generate(GenerateOptions(domain="pluniversity.edu.pl"))
    assert address.value.endswith("@pluniversity.edu.pl")
    (create,) = sent(http, "POST", f"{EG}/create")
    assert create["data"]["domain"] == "pluniversity.edu.pl"
    assert address.value == f"{create['data']['name']}@pluniversity.edu.pl"
    assert json.loads(address.state)["cookie"] == "emailgenerator-named"


async def test_a_chosen_local_part_is_created():
    http = FakeTransport("emailtemp", routes())
    address = await EmailTemp(http).generate(GenerateOptions(local="hello", domain="tormails.com"))
    assert address.value == "hello@tormails.com"
    assert json.loads(address.state) == {"site": "emailtemp.org", "cookie": "emailtemp-named"}
    (create,) = sent(http, "POST", f"{ET}/create")
    assert create["data"] == {
        "_token": "fixture-csrf-token",
        "name": "hello",
        "domain": "tormails.com",
    }
    # The mailbox cookie rides on the 302; following it lands on /en and loses it.
    assert create["allow_redirects"] is False
    # /create only works for a session that already holds a mailbox.
    assert create["cookies"] == {
        "emailtemp_session": "emailtemp-session",
        "email": "emailtemp-inbox-1",
    }


async def test_a_chosen_local_part_without_a_domain_takes_the_minted_domain():
    http = FakeTransport("emailtemp", routes())
    address = await EmailTemp(http).generate(GenerateOptions(local="hello"))
    # The first minted mailbox on either host: tormails.com or grnail.cam.
    assert address.value in {"hello@tormails.com", "hello@grnail.cam"}


async def test_a_taken_local_part_is_refused():
    # The site redirects back to /change and sets no mailbox cookie.
    http = FakeTransport(
        "emailtemp",
        routes(**{f"POST {ET}/create": Reply(302, "", headers={"Location": f"{ET}/change"})}),
    )
    with pytest.raises(NotSupported):
        await EmailTemp(http).generate(GenerateOptions(local="taken", domain="tormails.com"))


async def test_a_domain_no_host_serves_is_refused():
    with pytest.raises(NotSupported):
        await EmailTemp(FakeTransport("emailtemp", routes())).generate(
            GenerateOptions(domain="gmail.com")
        )


async def test_a_home_page_without_a_csrf_token_is_drift():
    http = FakeTransport("emailtemp", routes(**{f"GET {ET}/en": "<html></html>"}))
    with pytest.raises(SchemaDrift):
        await EmailTemp(http).generate(GenerateOptions(domain="tormails.com"))


async def test_a_mailbox_answer_without_the_email_cookie_is_drift():
    http = FakeTransport(
        "emailtemp", routes(**{f"POST {ET}/messages": raw("emailtemp_messages_new_1.json")})
    )
    with pytest.raises(SchemaDrift):
        await EmailTemp(http).generate(GenerateOptions(domain="tormails.com"))


async def test_the_mailbox_cookie_is_replayed_on_list_and_get():
    http = FakeTransport("emailtemp", routes())
    provider = EmailTemp(http)
    await provider.list(GENERATOR)
    await provider.get(GENERATOR, "SyntheticFixtureId000001")
    expected = {
        "emailgenerator_session": "emailgenerator-session",
        "email": "emailgenerator-inbox-1",
    }
    posts = sent(http, "POST", f"{EG}/messages")
    assert [kw["cookies"] for kw in posts] == [expected, expected]
    assert all(kw["data"]["_token"] == "fixture-csrf-token" for kw in posts)


async def test_list_reads_the_rows():
    rows = await EmailTemp(FakeTransport("emailtemp", routes())).list(EMAILTEMP)
    assert len(rows) == 1
    assert rows[0].id == "86J5Nvyk04lJdJk0RpBwGPzo"
    assert rows[0].sender == "Xeramail Test <test@xeramail.com>"
    assert rows[0].subject == "Your Email Test is Successful!"
    assert rows[0].received_at == datetime(2026, 9, 25, 3, 45, 37, tzinfo=UTC)


async def test_an_empty_inbox_is_an_empty_list():
    assert await EmailTemp(FakeTransport("emailtemp", routes())).list(EMPTY) == []


async def test_a_mailbox_the_site_no_longer_knows_is_expired():
    # An email cookie the site cannot read gets a brand-new mailbox, silently.
    http = FakeTransport(
        "emailtemp",
        routes(
            **{
                listing(ET, "emailtemp_session", "emailtemp", "inbox-2"): raw(
                    "emailtemp_messages_1.json"
                )
            }
        ),
    )
    with pytest.raises(AddressExpired):
        await EmailTemp(http).list(EMPTY)


@pytest.mark.parametrize(
    "answer",
    [
        "[]",
        '{"mailbox": "cbtb77b22c1@tormails.com"}',
        '{"mailbox": "cbtb77b22c1@tormails.com", "messages": "none"}',
        '{"mailbox": "cbtb77b22c1@tormails.com", "messages": [{"subject": "no id"}]}',
        '{"mailbox": "cbtb77b22c1@tormails.com", "messages": [{"id": "1", "receivedAt": "soon"}]}',
        "<html>Page Expired</html>",
    ],
)
async def test_a_listing_it_does_not_recognise_is_drift(answer):
    http = FakeTransport(
        "emailtemp", routes(**{listing(ET, "emailtemp_session", "emailtemp", "inbox-1"): answer})
    )
    with pytest.raises(SchemaDrift):
        await EmailTemp(http).list(EMAILTEMP)


@pytest.mark.parametrize("bad", [None, "", "not json", '{"site": "example.com", "cookie": "x"}'])
async def test_list_without_usable_state_is_refused(bad):
    provider = EmailTemp(FakeTransport("emailtemp", routes()))
    with pytest.raises(NotSupported):
        await provider.list(Address(EMAILTEMP.value, "emailtemp", state=bad))


async def test_get_takes_the_body_from_the_row():
    message = await EmailTemp(FakeTransport("emailtemp", routes())).get(
        EMAILTEMP, "86J5Nvyk04lJdJk0RpBwGPzo"
    )
    assert message.sender == "Xeramail Test <test@xeramail.com>"
    assert message.received_at == datetime(2026, 9, 25, 3, 45, 37, tzinfo=UTC)
    assert "Congratulations!" in message.html
    assert message.text.startswith("Congratulations!")


async def test_get_extracts_links():
    message = await EmailTemp(FakeTransport("emailtemp", routes())).get(
        GENERATOR, "SyntheticFixtureId000001"
    )
    assert message.links == ("https://example.test/confirm",)


async def test_a_message_the_listing_no_longer_has_is_gone():
    with pytest.raises(MessageGone):
        await EmailTemp(FakeTransport("emailtemp", routes())).get(EMAILTEMP, "nope")


async def test_a_row_without_a_body_is_drift():
    row = {"id": "1", "from": "a", "from_email": "a@x.test", "subject": "s", "content": None}
    answer = json.dumps({"mailbox": EMAILTEMP.value, "messages": [row]})
    http = FakeTransport(
        "emailtemp", routes(**{listing(ET, "emailtemp_session", "emailtemp", "inbox-1"): answer})
    )
    with pytest.raises(SchemaDrift):
        await EmailTemp(http).get(EMAILTEMP, "1")
