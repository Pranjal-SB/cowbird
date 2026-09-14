"""Hits real maildrop.cc. Skipped by default; this is the canary."""

import pytest
from cowbird.transport import Transport
from cowbird_maildrop import Maildrop

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("maildrop")
    try:
        yield Maildrop(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert await provider.list(address) == []
