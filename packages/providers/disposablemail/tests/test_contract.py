import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_disposablemail import DisposableMail

FIXTURES = Path(__file__).parent / "fixtures"
# host -> (inbox cookie, the one domain each host handed out during recon)
SITES = {
    "disposablemail.com": ("TMA", "dropoffs.org"),
    "fakemail.net": ("TMA", "forliion.com"),
    "minuteinbox.com": ("MI", "minafter.com"),
}
DM = "https://www.disposablemail.com"
FM = "https://www.fakemail.net"


def raw(name):
    # Bytes decoded by hand so a leading UTF-8 BOM survives, as it does in the
    # real response text.
    return (FIXTURES / name).read_bytes().decode("utf-8")


def host_routes(host):
    tag = host.split(".")[0]
    cookie, domain = SITES[host]
    base = f"https://www.{host}"
    return {
        f"GET {base}/?": Responses(
            Reply(200, raw(f"{tag}_home_1.html"), cookies={"PHPSESSID": f"{tag}-session-1"}),
            Reply(200, raw(f"{tag}_home_2.html"), cookies={"PHPSESSID": f"{tag}-session-2"}),
        ),
        f"GET {base}/index/index": Responses(
            Reply(200, raw(f"{tag}_index_1.json"), cookies={cookie: f"fixture.one%40{domain}"}),
            Reply(200, raw(f"{tag}_index_2.json"), cookies={cookie: f"fixture.two%40{domain}"}),
        ),
        f"GET {base}/index/refresh": raw(f"{tag}_refresh.json"),
        f"GET {base}/email/id/1": raw(f"{tag}_email_1.html"),
        f"GET {base}/email/id/": raw(f"{tag}_email_missing.html"),
    }


def routes(**overrides):
    merged = {}
    for host in SITES:
        merged.update(host_routes(host))
    return {**merged, **overrides}


def state(site, cookie):
    return json.dumps({"site": site, "cookie": cookie}, separators=(",", ":"))


DISPOSABLE = Address(
    "fixture.one@dropoffs.org",
    "disposablemail",
    state=state("disposablemail.com", "fixture.one%40dropoffs.org"),
)
FAKEMAIL = Address(
    "fixture.one@forliion.com",
    "disposablemail",
    state=state("fakemail.net", "fixture.one%40forliion.com"),
)


def sent(http, url):
    return [kw for _, u, kw in http.seen if u == url]


class TestDisposableMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return DisposableMail(FakeTransport("disposablemail", routes()))


def test_it_asks_for_a_fresh_session_per_request():
    # On a shared jar the site answers a second generate() with the first
    # address, because the TMA cookie from the first is still in the jar.
    assert DisposableMail.caps.fresh_session is True


def test_it_covers_every_host_of_the_engine():
    assert set(DisposableMail.caps.sites) == set(SITES)


async def test_generate_replays_the_home_session_and_csrf_into_index():
    http = FakeTransport("disposablemail", routes())
    await DisposableMail(http).generate(GenerateOptions(domain="dropoffs.org"))
    (index,) = sent(http, f"{DM}/index/index")
    assert index["params"] == {"csrf_token": "0" * 64}
    assert index["cookies"] == {"PHPSESSID": "disposablemail-session-1"}
    assert index["headers"]["X-Requested-With"] == "XMLHttpRequest"


async def test_generate_picks_the_host_that_serves_the_domain():
    provider = DisposableMail(FakeTransport("disposablemail", routes()))
    before = datetime.now(UTC)
    minute = await provider.generate(GenerateOptions(domain="minafter.com"))
    hour = await provider.generate(GenerateOptions(domain="dropoffs.org"))
    assert minute.value == "fixture.one@minafter.com"
    assert hour.value == "fixture.one@dropoffs.org"
    assert before + timedelta(minutes=9) < minute.expires_at <= before + timedelta(minutes=11)
    assert before + timedelta(minutes=59) < hour.expires_at <= before + timedelta(minutes=61)


async def test_a_domain_no_host_serves_is_refused():
    provider = DisposableMail(FakeTransport("disposablemail", routes()))
    with pytest.raises(NotSupported):
        await provider.generate(GenerateOptions(domain="gmail.com"))


async def test_a_chosen_local_part_is_refused():
    provider = DisposableMail(FakeTransport("disposablemail", routes()))
    with pytest.raises(NotSupported):
        await provider.generate(GenerateOptions(local="hello"))


async def test_a_bom_prefixed_answer_parses():
    http = FakeTransport("disposablemail", routes())
    provider = DisposableMail(http)
    address = await provider.generate(GenerateOptions(domain="forliion.com"))
    assert address.value == "fixture.one@forliion.com"
    rows = await provider.list(address)
    assert [(r.id, r.subject) for r in rows] == [("1", "Welcome to FakeMail:)")]


async def test_the_inbox_cookie_from_generate_is_sent_on_list_and_get():
    http = FakeTransport("disposablemail", routes())
    provider = DisposableMail(http)
    address = await provider.generate(GenerateOptions(domain="minafter.com"))
    await provider.get(address, "1")
    expected = {"MI": "fixture.one%40minafter.com"}
    base = "https://www.minuteinbox.com"
    assert [kw["cookies"] for kw in sent(http, f"{base}/index/refresh")] == [expected]
    assert [kw["cookies"] for kw in sent(http, f"{base}/email/id/1")] == [expected]


async def test_an_index_answer_without_the_inbox_cookie_is_drift():
    http = FakeTransport(
        "disposablemail", routes(**{f"GET {DM}/index/index": raw("disposablemail_index_1.json")})
    )
    with pytest.raises(SchemaDrift):
        await DisposableMail(http).generate(GenerateOptions(domain="dropoffs.org"))


async def test_a_home_page_without_a_csrf_token_is_drift():
    http = FakeTransport("disposablemail", routes(**{f"GET {DM}/?": "<html></html>"}))
    with pytest.raises(SchemaDrift):
        await DisposableMail(http).generate(GenerateOptions(domain="dropoffs.org"))


async def test_list_reads_the_rows():
    rows = await DisposableMail(FakeTransport("disposablemail", routes())).list(DISPOSABLE)
    assert len(rows) == 1
    assert rows[0].id == "1"
    assert rows[0].sender == "Disposable Mail Address <Admin@DisposableMail.com>"
    assert rows[0].subject == "Welcome to DisposableMail:)"
    # The site only says "13 sec. ago".
    assert rows[0].received_at is None


async def test_an_emptied_inbox_is_an_empty_list():
    http = FakeTransport(
        "disposablemail", routes(**{f"GET {FM}/index/refresh": raw("fakemail_refresh_empty.json")})
    )
    assert await DisposableMail(http).list(FAKEMAIL) == []


async def test_false_from_refresh_is_an_empty_list():
    # The site's own script treats `false` as "no mail". This body is the
    # engine's real `false` (BOM included), as recorded from its email endpoint.
    http = FakeTransport(
        "disposablemail", routes(**{f"GET {FM}/index/refresh": raw("fakemail_email_false.json")})
    )
    assert await DisposableMail(http).list(FAKEMAIL) == []


async def test_an_inbox_the_site_no_longer_knows_is_expired():
    http = FakeTransport(
        "disposablemail",
        routes(**{f"GET {DM}/index/refresh": raw("disposablemail_refresh_unknown.json")}),
    )
    with pytest.raises(AddressExpired):
        await DisposableMail(http).list(DISPOSABLE)


@pytest.mark.parametrize(
    "answer",
    ['{"email": "changed"}', '[{"od": "no id"}]', '"ok"', "<html>maintenance</html>"],
)
async def test_a_listing_it_does_not_recognise_is_drift(answer):
    http = FakeTransport("disposablemail", routes(**{f"GET {DM}/index/refresh": answer}))
    with pytest.raises(SchemaDrift):
        await DisposableMail(http).list(DISPOSABLE)


@pytest.mark.parametrize("bad", [None, "", "not json", '{"site": "example.com", "cookie": "x"}'])
async def test_list_without_usable_state_is_refused(bad):
    provider = DisposableMail(FakeTransport("disposablemail", routes()))
    with pytest.raises(NotSupported):
        await provider.list(Address(DISPOSABLE.value, "disposablemail", state=bad))


async def test_get_reads_the_body_and_takes_the_headers_from_the_row():
    message = await DisposableMail(FakeTransport("disposablemail", routes())).get(FAKEMAIL, "1")
    assert message.id == "1"
    assert message.sender == "Fake Mail <Admin@FakeMail.net>"
    assert message.subject == "Welcome to FakeMail:)"
    assert not message.html.startswith("﻿")
    assert message.text.startswith("Your private mailbox is protected")
    assert "https://www.fakemail.net/" in message.links


async def test_a_message_the_listing_no_longer_has_is_gone():
    with pytest.raises(MessageGone):
        await DisposableMail(FakeTransport("disposablemail", routes())).get(DISPOSABLE, "2")


async def test_a_body_the_site_cannot_load_is_gone():
    http = FakeTransport(
        "disposablemail",
        routes(**{f"GET {FM}/email/id/1": raw("fakemail_email_missing.html")}),
    )
    with pytest.raises(MessageGone):
        await DisposableMail(http).get(FAKEMAIL, "1")
