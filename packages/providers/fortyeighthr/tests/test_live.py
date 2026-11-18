"""Hits real 48hr.email. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means 48hr.email changed,
or the runner's egress is blocked. Read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_fortyeighthr import FortyEightHr

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("fortyeighthr")
    try:
        yield FortyEightHr(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert address.value.split("@")[1] in provider.caps.domains
    assert address.state is None
    assert await provider.list(address) == []


async def test_seeded_example_inbox_reads_end_to_end(provider):
    from cowbird.models import Address

    address = Address("example@48hr.email", "fortyeighthr")
    rows = await provider.list(address)
    assert rows, "the official instance seeds example@48hr.email; it was empty"
    message = await provider.get(address, rows[0].id)
    assert message.id == rows[0].id
    assert message.text or message.html
