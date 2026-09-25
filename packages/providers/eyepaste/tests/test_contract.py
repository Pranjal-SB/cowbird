from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport
from cowbird_eyepaste import Eyepaste

FIXTURES = Path(__file__).parent / "fixtures"
ADDRESS = Address("cb3635adc9019e@eyepaste.com", "eyepaste")

# Synthetic, not recorded: a single-part HTML mail in the recorded item shape.
# The feed drops the mail's own headers, so no boundary line opens the body.
SINGLE_PART = """<rss version='2.0'><channel><title>x</title>
<item>
<title><![CDATA[ Sender <s@example.test>: Hello there ]]></title>
<link>https://www.eyepaste.com/inbox/x@eyepaste.com</link>
<description>
<![CDATA[
    <p>
      From: Sender <s@example.test>
      <br/>
      To: x@eyepaste.com
      <br/>
      Subject: Hello there
      <br/>
      Date: Thu, 24 Sep 2026 22:58:55 +0700
      <br/>
    </p>
    <p>
      <html><br/><body><br/><p>Your code is 123456</p><br/><a href="https://example.test/go">go</a><br/></body><br/></html>
    </p>
]]>
</description>
<pubdate>
Thu, 24 Sep 2026 22:58:55 +0700
</pubdate>
</item></channel></rss>"""


def feed(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    return {"GET https://www.eyepaste.com/inbox/": feed("inbox.rss"), **overrides}


def eyepaste(**overrides):
    return Eyepaste(FakeTransport("eyepaste", routes(**overrides)))


class TestEyepasteContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return eyepaste()


async def test_the_inbox_feed_is_read_by_address():
    http = FakeTransport("eyepaste", routes())
    await Eyepaste(http).list(ADDRESS)
    assert http.seen[-1][1] == f"https://www.eyepaste.com/inbox/{ADDRESS.value}.rss"


async def test_a_row_carries_sender_subject_and_utc_date():
    [row] = await eyepaste().list(ADDRESS)
    assert row.sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert row.subject == "JoltMx test email (ref 01a0d69e20c2)"
    assert row.received_at == datetime(2026, 9, 25, 3, 31, 22, tzinfo=UTC)


async def test_the_derived_id_is_stable_across_reads():
    first = await eyepaste().list(ADDRESS)
    second = await eyepaste().list(ADDRESS)
    assert first[0].id and first[0].id == second[0].id


async def test_an_empty_feed_is_an_empty_list():
    provider = eyepaste(**{"GET https://www.eyepaste.com/inbox/": feed("inbox_empty.rss")})
    assert await provider.list(ADDRESS) == []


async def test_get_splits_the_multipart_body():
    provider = eyepaste()
    [row] = await provider.list(ADDRESS)
    message = await provider.get(ADDRESS, row.id)
    assert message.id == row.id
    assert message.subject == row.subject
    assert "everything is working as expected" in message.text
    assert "Content-Type" not in message.text
    assert "<h2" in message.html
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)


async def test_the_double_encoded_feed_is_repaired():
    provider = eyepaste()
    [row] = await provider.list(ADDRESS)
    message = await provider.get(ADDRESS, row.id)
    assert "JoltMx — email routing" in message.text


async def test_a_single_part_html_body_is_kept_as_html():
    provider = eyepaste(**{"GET https://www.eyepaste.com/inbox/": SINGLE_PART})
    [row] = await provider.list(ADDRESS)
    message = await provider.get(ADDRESS, row.id)
    assert row.received_at == datetime(2026, 9, 24, 15, 58, 55, tzinfo=UTC)
    assert "123456" in message.text
    assert "<br/>" not in message.html
    assert message.links == ("https://example.test/go",)


async def test_a_part_in_a_charset_python_does_not_know_is_drift_not_a_crash():
    unknown = feed("inbox.rss").replace("charset=utf-8", "charset=x-no-such-charset")
    provider = eyepaste(**{"GET https://www.eyepaste.com/inbox/": unknown})
    [row] = await provider.list(ADDRESS)
    with pytest.raises(SchemaDrift):
        await provider.get(ADDRESS, row.id)


async def test_an_id_no_longer_in_the_feed_is_gone():
    with pytest.raises(MessageGone):
        await eyepaste().get(ADDRESS, "0123456789abcdef")


async def test_a_feed_that_is_not_xml_is_drift():
    provider = eyepaste(**{"GET https://www.eyepaste.com/inbox/": "<html>oops"})
    with pytest.raises(SchemaDrift):
        await provider.list(ADDRESS)


async def test_generate_honours_a_chosen_local_part():
    address = await eyepaste().generate(GenerateOptions(local="hello", domain="eyepaste.com"))
    assert address.value == "hello@eyepaste.com"


async def test_a_generated_local_part_is_long_enough_to_be_unguessable():
    address = await eyepaste().generate()
    assert len(address.value.split("@")[0]) >= 18


async def test_a_domain_it_does_not_serve_is_refused():
    with pytest.raises(NotSupported):
        await eyepaste().generate(GenerateOptions(domain="gmail.com"))
