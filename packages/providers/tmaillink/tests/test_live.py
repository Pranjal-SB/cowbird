"""Hits the real tmail.link. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means the backend moved.
tmail.link is a self-hosted Django app with no gate; the most likely breakage
is the site simply being down.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_tmaillink import TMailLink

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("tmaillink")
    try:
        yield TMailLink(http)
    finally:
        await http.aclose()


async def test_generate_list_and_read_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert address.value.endswith("@tmail.link")

    rows = await provider.list(address)
    assert isinstance(rows, list)
    assert all(row.id for row in rows)
