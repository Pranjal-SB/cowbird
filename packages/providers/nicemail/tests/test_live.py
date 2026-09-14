"""Hits real nicemail.cc / web.mailporary.com. Skipped by default; the canary."""

import pytest
from cowbird.transport import Transport
from cowbird_nicemail import NiceMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("nicemail")
    try:
        yield NiceMail(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert await provider.list(address) == []
