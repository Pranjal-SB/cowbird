"""Hits real tmailor.com. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means tmailor changed,
or the runner's egress is blocked. Read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_tmailor import Tmailor

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("tmailor")
    try:
        yield Tmailor(http)
    finally:
        await http.aclose()


async def test_generate_list_get_delete_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    assert await provider.list(address) == []
