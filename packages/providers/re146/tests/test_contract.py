import hashlib
import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply
from cowbird_re146 import Re146

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "6814ac84-62ad-4619-96a0-2e44d99b7438"
ADDRESS = Address("cbbe51e1e579@zenvixsmp.xyz", "re146")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def routes(**overrides):
    return {
        "/api/domains": load("domains.json"),
        "/api/messages/": load("messages.json"),
        "/storage/": (FIXTURES / "message.eml").read_text(encoding="utf-8"),
        **overrides,
    }


class TestRe146Contract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return Re146(FakeTransport("re146", routes()))


async def test_the_inbox_is_keyed_by_the_md5_of_the_address():
    http = FakeTransport("re146", routes())
    await Re146(http).list(ADDRESS)
    key = hashlib.md5(ADDRESS.value.encode()).hexdigest()
    assert any(url.endswith(f"/api/messages/{key}") for _, url, _ in http.seen)


async def test_a_custom_local_part_and_a_served_domain_are_honoured():
    address = await Re146(FakeTransport("re146", routes())).generate(
        GenerateOptions(local="chosen", domain="re146.dev")
    )
    assert address.value == "chosen@re146.dev"


async def test_a_domain_it_does_not_serve_is_refused():
    with pytest.raises(NotSupported):
        await Re146(FakeTransport("re146", routes())).generate(
            GenerateOptions(domain="gmail.com")
        )


async def test_the_body_is_parsed_out_of_the_raw_source():
    message = await Re146(FakeTransport("re146", routes())).get(ADDRESS, MESSAGE_ID)
    assert message.subject.startswith("JoltMx test email")
    assert "everything is working as expected" in message.text
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)


async def test_a_404_from_storage_is_gone():
    http = FakeTransport("re146", routes(**{"/storage/": Reply(404, "{}")}))
    with pytest.raises(MessageGone):
        await Re146(http).get(ADDRESS, MESSAGE_ID)


async def test_a_body_in_a_charset_python_does_not_know_is_drift_not_a_crash():
    source = (
        "From: a@b.test\nSubject: x\nContent-Type: text/html; charset=x-no-such-charset\n\n"
        "<p>hi</p>\n"
    )
    http = FakeTransport("re146", routes(**{"/storage/": source}))
    with pytest.raises(SchemaDrift):
        await Re146(http).get(ADDRESS, MESSAGE_ID)


async def test_a_list_that_is_not_an_array_is_drift():
    http = FakeTransport("re146", routes(**{"/api/messages/": {"changed": True}}))
    with pytest.raises(SchemaDrift):
        await Re146(http).list(ADDRESS)
