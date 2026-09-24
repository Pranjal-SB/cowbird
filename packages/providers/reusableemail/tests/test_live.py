"""Hits real reusable.email. Skipped by default; this is the canary."""

import pytest
from cowbird.models import Address
from cowbird.transport import Transport
from cowbird_reusableemail import ReusableEmail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("reusableemail")
    try:
        yield ReusableEmail(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert await provider.list(address) == []


async def test_read_path_against_a_public_inbox_that_holds_mail(provider):
    # test@ is public and already holds mail, so the read path is proven
    # without sending anything.
    address = Address("test@reusable.email", provider.name)
    rows = await provider.list(address)
    assert rows, "test@reusable.email came back empty"
    message = await provider.get(address, rows[0].id)
    assert message.id == rows[0].id
    assert message.text
