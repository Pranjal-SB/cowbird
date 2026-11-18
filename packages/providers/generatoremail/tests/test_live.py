"""Hits the real emailfake.com. Skipped by default; this is the canary.

A failure here is not a regression in this repo — it means the backend moved,
or the runner's egress is gated (the 503 proof-of-work interstitial; see the
task 6 report for the gating reality). Read the error type before assuming which.
"""

import pytest
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_generatoremail import GeneratorEmail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport(
        "generatoremail",
        fresh_session=GeneratorEmail.caps.fresh_session,
        max_concurrency=GeneratorEmail.caps.max_concurrency,
    )
    try:
        yield GeneratorEmail(http)
    finally:
        await http.aclose()


async def test_generate_list_and_read_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    assert address.state  # the verified-session cookie travels in state

    rows = await provider.list(address)
    assert isinstance(rows, list)
    assert all(row.id for row in rows)

    # A busy public inbox on a served domain reads end to end with no sender.
    busy = Address("info@puxa.top", "generatoremail")
    public_rows = await provider.list(busy)
    if public_rows:
        message = await provider.get(busy, public_rows[0].id)
        assert message.id == public_rows[0].id
