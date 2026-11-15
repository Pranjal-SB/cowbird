"""Hits the real tempemail.cc. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means the backend moved.
Note the create cooldown: ~1 account per IP per cooldown window, so this test
generates exactly one account and then resumes it from Address.state.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_tempemailcc import TempEmailCc

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("tempemailcc")
    try:
        yield TempEmailCc(http)
    finally:
        await http.aclose()


async def test_generate_list_read_and_resume(provider):
    address = await provider.generate(GenerateOptions())
    assert address.value.endswith("@icmans.com")

    rows = await provider.list(address)
    assert isinstance(rows, list)
    assert all(row.id for row in rows)

    if rows:
        message = await provider.get(address, rows[0].id)
        assert message.id == rows[0].id
        assert isinstance(message.text, str)

    # The state alone must be enough for a fresh provider object — that is the
    # `cowbird wait --state` contract. (A second generate() here would trip the
    # 1-create-per-cooldown-window rate limit.)
    http2 = Transport("tempemailcc")
    try:
        resumed = TempEmailCc(http2)
        assert isinstance(await resumed.list(address), list)
    finally:
        await http2.aclose()
