"""Hits real anonbox.net. Skipped by default; delivery proof lives here."""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_anonbox import Anonbox

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("anonbox")
    try:
        yield Anonbox(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert address.value.endswith(".anonbox.net")
    assert address.state
    assert await provider.list(address) == []
