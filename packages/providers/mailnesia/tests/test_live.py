"""Hits real mailnesia.com. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means mailnesia changed,
or the runner's egress is blocked. Read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_mailnesia import Mailnesia

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("mailnesia")
    try:
        yield Mailnesia(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    # The random local part is never an alias, so a fresh inbox is genuinely empty.
    assert await provider.list(address) == []
