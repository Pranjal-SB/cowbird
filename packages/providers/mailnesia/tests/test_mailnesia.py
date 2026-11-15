from pathlib import Path

import pytest
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply
from cowbird_mailnesia import Mailnesia

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text()


def fake(routes, status=200):
    return FakeTransport("mailnesia", routes, status)


def routes_for(local="cbprobe"):
    return {
        f"/mailbox/{local}?": load("mailbox.html"),
        # Longest-key-first matching: the bare-id key catches the message page
        # whatever local part generate() coined, and the /mailbox/ fallback
        # catches the inbox listing for random locals.
        "1299827515": load("message.html"),
        "9999999999": Reply(404, ""),
        "/mailbox/": load("mailbox.html"),
        f"/rss/{local}?": load("rss.xml"),
    }


DEFAULT_ROUTES = routes_for()


async def test_generate_needs_no_network_and_picks_an_unaliased_local():
    # generate() is purely local arithmetic: any local part @mailnesia.com
    # exists implicitly, so no request is needed. The random local part must
    # be lowercase and short enough for mailnesia's <=30 char rule.
    http = fake(dict(DEFAULT_ROUTES))
    address = await Mailnesia(http).generate(GenerateOptions())
    assert http.seen == []
    assert address.value.endswith("@mailnesia.com")
    local = address.value.split("@")[0]
    assert local.islower() and len(local) <= 30
    assert address.state is None  # nothing to carry: the address IS the state


async def test_generate_honors_custom_local_and_domain():
    address = await Mailnesia(fake(dict(DEFAULT_ROUTES))).generate(
        GenerateOptions(local="myprobe", domain="poofmail.fit")
    )
    assert address.value == "myprobe@poofmail.fit"


async def test_generate_refuses_a_local_part_mailnesia_would_not_serve():
    # Core allows [A-Za-z0-9._+-]{1,64}; mailnesia's own rule is
    # [a-z0-9._+-]{1,30}. Minting a local list() would refuse would hand the
    # caller an address that errors on first use.
    with pytest.raises(NotSupported):
        await Mailnesia(fake(dict(DEFAULT_ROUTES))).generate(
            GenerateOptions(local="MyProbe")
        )
    with pytest.raises(NotSupported):
        await Mailnesia(fake(dict(DEFAULT_ROUTES))).generate(
            GenerateOptions(local="a" * 31)
        )


async def test_generate_refuses_a_domain_outside_the_rotation():
    # mailnesia serves mailnesia.com plus its five yearly-rotating alternates;
    # BYOD exists upstream but this adapter cannot verify MX, so refuse.
    with pytest.raises(NotSupported):
        await Mailnesia(fake(dict(DEFAULT_ROUTES))).generate(
            GenerateOptions(domain="not-a-real-domain.test")
        )


async def test_list_parses_the_server_rendered_inbox_table():
    rows = await Mailnesia(fake(dict(DEFAULT_ROUTES))).list(
        Address("cbprobe@mailnesia.com", "mailnesia")
    )
    assert [(r.id, r.sender, r.subject) for r in rows] == [
        ("1299827515", "noreply@example.test", "Your verification code is 482913."),
        ("1299533678", "noreply@example.test", "Your verification code is 482913."),
    ]
    assert rows[0].received_at is not None


async def test_list_on_an_empty_mailbox_is_an_empty_list():
    http = fake({"/mailbox/emptyprobe?": load("mailbox_empty.html")})
    rows = await Mailnesia(http).list(Address("emptyprobe@mailnesia.com", "mailnesia"))
    assert rows == []


async def test_list_surfaces_the_alias_warning():
    # Common local parts (test, admin, info...) are aliases of other mailboxes:
    # mailnesia renders a warning and no messages. That address can never
    # receive for the caller, so it must not masquerade as a healthy empty
    # inbox.
    http = fake({"/mailbox/test?": load("mailbox_alias.html")})
    with pytest.raises(NotSupported, match="alias"):
        await Mailnesia(http).list(Address("test@mailnesia.com", "mailnesia"))


async def test_list_with_a_missing_table_raises_schema_drift():
    # No <table class="email"> at all means the page shape moved.
    http = fake({"/mailbox/brokenprobe?": "<html><body>moved</body></html>"})
    with pytest.raises(SchemaDrift):
        await Mailnesia(http).list(Address("brokenprobe@mailnesia.com", "mailnesia"))


async def test_get_parses_the_message_page():
    message = await Mailnesia(fake(dict(DEFAULT_ROUTES))).get(
        Address("cbprobe@mailnesia.com", "mailnesia"), "1299827515"
    )
    assert message.id == "1299827515"
    assert message.sender == "noreply@example.test"
    assert message.subject == "Your verification code is 482913."
    assert message.received_at is not None
    assert "482913" in message.text
    assert "https://example.test/verify?code=482913" in message.links


async def test_get_on_a_missing_message_is_gone_not_drift():
    http = fake(dict(DEFAULT_ROUTES))
    with pytest.raises(MessageGone):
        await Mailnesia(http).get(
            Address("cbprobe@mailnesia.com", "mailnesia"), "9999999999"
        )


async def test_rss_lists_the_same_inbox_from_the_feed_side():
    rows = await Mailnesia(fake(dict(DEFAULT_ROUTES))).rss(
        Address("cbprobe@mailnesia.com", "mailnesia"), count=3
    )
    assert [r.id for r in rows] == ["1299827515", "1299533678"]
    assert all(r.subject == "Your verification code is 482913." for r in rows)
    # <pubDate> feeds received_at; a malformed date degrades to None.
    assert rows[0].received_at is not None
    assert rows[1].received_at is not None
