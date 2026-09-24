"""Hits real tempmailhub.org. Skipped by default; this is the canary.

The inboxes are shared Gmail accounts that already hold other people's mail,
so the read path is exercised without sending anything.
"""

import pytest
from cowbird.transport import Transport
from cowbird_tempmailhub import TempMailHub

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("tempmailhub", max_concurrency=1)
    try:
        yield TempMailHub(http)
    finally:
        await http.aclose()


async def test_generate_list_and_read_against_the_real_service(provider):
    address = await provider.generate()
    assert address.value.endswith("@gmail.com")
    rows = await provider.list(address)
    assert isinstance(rows, list)
    if not rows:
        pytest.skip("tempmailhub: inbox empty; body-read path NOT tested")
    message = await provider.get(address, rows[0].id)
    assert message.id == rows[0].id
    assert message.text.strip()
