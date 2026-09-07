"""Hits real emailnator.com. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means emailnator changed,
or the runner's egress drew a Cloudflare challenge. `CloudflareChallenge` means
the second; read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_emailnator import Emailnator

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("emailnator")
    try:
        yield Emailnator(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert address.value.endswith("@gmail.com")

    # A fresh emailnator address is NOT empty: the service seeds every inbox
    # with its own locked promo message advertising the paid tier. Asserting an
    # empty list here fails against a perfectly healthy backend. Verified live
    # 2026-09-07.
    rows = await provider.list(address)
    assert isinstance(rows, list)
    assert all(row.id for row in rows)
