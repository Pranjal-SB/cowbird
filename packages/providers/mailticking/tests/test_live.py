"""Hits real mailticking.com. Skipped by default; this is the canary."""

import pytest
from cowbird.transport import Transport
from cowbird_mailticking import MailTicking

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("mailticking", max_concurrency=1)
    try:
        yield MailTicking(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert address.state
    assert await provider.list(address) == []
