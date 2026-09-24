"""Hits real 22.do. Skipped by default; this is the canary."""

import pytest
from cowbird.models import Kind
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_twentytwodo import TwentyTwoDo

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("22do")
    try:
        yield TwentyTwoDo(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert address.state
    # Not `== []`: Gmail and Outlook aliases are shared, and a fresh one can
    # already hold mail someone else received.
    assert isinstance(await provider.list(address), list)


async def test_a_gmail_address_can_be_asked_for(provider):
    address = await provider.generate(GenerateOptions(kind=Kind.GMAIL_ALIAS))
    assert address.value.endswith(("@gmail.com", "@googlemail.com"))
