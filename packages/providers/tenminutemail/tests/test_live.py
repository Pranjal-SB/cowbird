"""Hits real 10minutemail.com. Skipped by default; this is the canary."""

import pytest
from cowbird.transport import Transport
from cowbird_tenminutemail import TenMinuteMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("10minutemail", fresh_session=True)
    try:
        yield TenMinuteMail(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert address.state
    assert await provider.list(address) == []


async def test_two_addresses_on_one_provider_do_not_share_an_inbox(provider):
    assert (await provider.generate()).value != (await provider.generate()).value
