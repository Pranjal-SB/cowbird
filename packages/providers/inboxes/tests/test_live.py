"""Hits real inboxes.com. Skipped by default; this is the canary.

A failure here is not a regression in this repo -- it means inboxes.com changed.
Read the error type before assuming which.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_inboxes import DOMAINS, Inboxes

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("inboxes")
    try:
        yield Inboxes(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert address.value.split("@")[1] in DOMAINS
    # A never-before-seen catch-all address answers 200 {"msgs": []}, not a 404.
    assert await provider.list(address) == []


async def test_the_declared_domain_list_still_matches_the_live_one(provider):
    # generate() never contacts the server, so a stale domain here yields
    # addresses that silently receive nothing. This is the only check.
    payload = await provider.http.json("GET", "https://inboxes.com/api/v2/domain")
    live = tuple(sorted(d["qdn"] for d in payload["domains"]))
    assert live == tuple(sorted(DOMAINS)), (
        f"inboxes.com domain list moved: {set(live) ^ set(DOMAINS)}"
    )
