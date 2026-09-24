"""Hits real mail.re146.dev. Skipped by default; this is the canary."""

import pytest
from cowbird.transport import Transport
from cowbird_re146 import Re146

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("re146")
    try:
        yield Re146(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert await provider.list(address) == []
