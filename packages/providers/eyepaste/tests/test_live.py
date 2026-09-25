"""Hits real eyepaste.com. Skipped by default; this is the canary."""

import pytest
from cowbird.models import Address
from cowbird.transport import Transport
from cowbird_eyepaste import Eyepaste

pytestmark = pytest.mark.live

# A shared public inbox that has held mail whenever it was checked. Read here
# only to prove the body path; nothing from it is recorded or committed.
SHARED = Address("info@eyepaste.com", "eyepaste")


@pytest.fixture
async def provider():
    http = Transport("eyepaste")
    try:
        yield Eyepaste(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert await provider.list(address) == []


async def test_a_shared_inbox_row_reads_with_text(provider):
    rows = await provider.list(SHARED)
    if not rows:
        pytest.skip("info@eyepaste.com is empty right now; body-read path NOT tested")
    message = await provider.get(SHARED, rows[0].id)
    assert message.text.strip()
