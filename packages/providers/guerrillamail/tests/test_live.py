"""Hits real guerrillamail. Skipped by default; this is the canary."""

import pytest
from cowbird.transport import Transport
from cowbird_guerrillamail import GuerrillaMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("guerrillamail", fresh_session=True)
    try:
        yield GuerrillaMail(http)
    finally:
        await http.aclose()


async def test_every_fresh_inbox_can_be_read(provider):
    # Every new guerrillamail inbox holds a welcome message, so the body-read
    # path is exercised live with no sender at all.
    address = await provider.generate()
    rows = await provider.list(address)
    assert rows
    message = await provider.get(address, rows[0].id)
    assert message.text


async def test_two_addresses_on_one_provider_do_not_share_an_inbox(provider):
    first = await provider.generate()
    second = await provider.generate()
    assert first.value != second.value
