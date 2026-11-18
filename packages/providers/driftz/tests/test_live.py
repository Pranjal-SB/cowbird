"""Hits real driftz. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means driftz changed, or
the runner's egress is blocked. Read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_driftz import Driftz

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("driftz")
    try:
        yield Driftz(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    assert address.expires_at is not None
    assert await provider.list(address) == []


async def test_list_any_public_address(provider):
    from cowbird.models import Address

    rows = await provider.list(Address("cowbird-no-such-mailbox-probe@driftz.net", "driftz"))
    assert isinstance(rows, list)
