"""Hits real meowmail.in. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means meowmail changed,
or the runner's egress is blocked. Read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_meowmail import MeowMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("meowmail")
    try:
        yield MeowMail(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    assert address.value.split("@")[1] in provider.caps.domains
    assert await provider.list(address) == []


async def test_custom_local_part_is_honoured(provider):
    address = await provider.generate(GenerateOptions(local="cowbird-live-probe"))
    assert address.value == "cowbird-live-probe@meowmail.in"
