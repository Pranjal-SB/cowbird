"""Hits real anonymmail.net. Skipped by default; this is the canary."""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_anonymmail import AnonymMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("anonymmail")
    try:
        yield AnonymMail(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    assert await provider.list(address) == []
