"""Hits the real sites. Skipped by default; this is the canary.

Every new inbox already holds the site's welcome mail as id 1, so the read
path is proven without sending anything.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_disposablemail import DisposableMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("disposablemail", max_concurrency=1, fresh_session=True)
    try:
        yield DisposableMail(http)
    finally:
        await http.aclose()


@pytest.mark.parametrize("domain", ["dropoffs.org", "forliion.com", "minafter.com"])
async def test_generate_list_and_read_the_welcome_mail(provider, domain):
    address = await provider.generate(GenerateOptions(domain=domain))
    assert address.value.endswith(f"@{domain}")
    rows = await provider.list(address)
    assert "1" in [r.id for r in rows]
    message = await provider.get(address, "1")
    assert message.text


async def test_two_generates_are_two_inboxes(provider):
    first = await provider.generate(GenerateOptions(domain="dropoffs.org"))
    second = await provider.generate(GenerateOptions(domain="dropoffs.org"))
    assert first.value != second.value
