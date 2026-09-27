"""Hits the real sites. Skipped by default; this is the canary.

A new mailbox starts empty, so the read path is covered by the recorded
fixtures, not here.
"""

import secrets

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_emailtemp import EmailTemp

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("emailtemp", max_concurrency=1, fresh_session=True)
    try:
        yield EmailTemp(http)
    finally:
        await http.aclose()


# One domain per host; pluniversity.edu.pl also exercises /create, since the
# random mailbox usually lands on one of emailgenerator's other five domains.
@pytest.mark.parametrize("domain", ["tormails.com", "pluniversity.edu.pl"])
async def test_generate_then_the_inbox_is_empty(provider, domain):
    address = await provider.generate(GenerateOptions(domain=domain))
    assert address.value.endswith(f"@{domain}")
    assert await provider.list(address) == []


async def test_a_chosen_local_part(provider):
    local = "cbt" + secrets.token_hex(4)
    address = await provider.generate(GenerateOptions(local=local, domain="tormails.com"))
    assert address.value == f"{local}@tormails.com"
    assert await provider.list(address) == []


async def test_two_generates_are_two_inboxes(provider):
    first = await provider.generate(GenerateOptions(domain="tormails.com"))
    second = await provider.generate(GenerateOptions(domain="tormails.com"))
    assert first.value != second.value
