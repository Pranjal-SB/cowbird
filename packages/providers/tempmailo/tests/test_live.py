"""Hits the real site. Skipped by default; this is the canary.

A new address starts empty, so the read path is covered by the recorded
fixtures, not here.
"""

import pytest
from cowbird.transport import Transport
from cowbird_tempmailo import TempMailo

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("tempmailo", max_concurrency=TempMailo.caps.max_concurrency)
    try:
        yield TempMailo(http)
    finally:
        await http.aclose()


async def test_generate_then_the_inbox_is_empty(provider):
    address = await provider.generate()
    assert "@" in address.value
    assert await provider.list(address) == []
