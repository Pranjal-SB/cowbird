"""Hits real temp-mail.org. Skipped by default; this is the canary.

Mailbox creation is limited to 10 per IP, shared with 10minemail.com. This file
creates two; do not add more generate() calls to it.
"""

import pytest
from cowbird.transport import Transport
from cowbird_tempmailorg import TempMailOrg

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("tempmailorg", max_concurrency=2)
    try:
        yield TempMailOrg(http)
    finally:
        await http.aclose()


async def test_generate_list_and_distinct_addresses(provider):
    first = await provider.generate()
    second = await provider.generate()
    assert first.value != second.value
    assert await provider.list(first) == []
