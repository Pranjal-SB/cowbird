"""Hits real mail.cx. Skipped by default; this is the canary.

list() on an empty inbox holds for about 25 seconds: that is the backend's
long-poll, not a hang.
"""

import pytest
from cowbird.transport import Transport
from cowbird_mailcx import MailCx

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("mailcx", max_concurrency=1)
    try:
        yield MailCx(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert await provider.list(address) == []
