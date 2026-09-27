"""Hits the real site. Skipped by default; this is the canary."""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_mtempmail import MTempMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("mtempmail", max_concurrency=1, fresh_session=True)
    try:
        yield MTempMail(http)
    finally:
        await http.aclose()


async def test_generate_then_the_inbox_is_empty(provider):
    address = await provider.generate()
    assert await provider.list(address) == []


async def test_two_generates_are_two_inboxes(provider):
    first = await provider.generate()
    second = await provider.generate()
    assert first.value != second.value


async def test_a_chosen_edu_address(provider):
    address = await provider.generate(GenerateOptions(domain="zub.edu.pl"))
    assert address.value.endswith("@zub.edu.pl")
    assert await provider.list(address) == []
