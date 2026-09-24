"""Hits real DuckMail (api.duckmail.sbs). Skipped by default; this is the canary.

A failure here is not a regression in this repo: it means DuckMail changed, or
the runner's egress is blocked. Read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_duckmail import DuckMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("duckmail")
    try:
        yield DuckMail(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    assert address.state
    assert await provider.list(address) == []
