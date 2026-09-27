"""Hits real receivemail.org. Skipped by default; this is the canary.

test@getsomail.com is a well-known shared inbox that always holds mail, so the
read path is proven without sending anything. Nothing read from it is kept.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_receivemail import ReceiveMail

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("receivemail", max_concurrency=1, fresh_session=True)
    try:
        yield ReceiveMail(http)
    finally:
        await http.aclose()


async def test_generate_and_list_against_the_real_service(provider):
    address = await provider.generate()
    assert address.state
    assert await provider.list(address) == []


async def test_two_generates_are_two_inboxes(provider):
    first = await provider.generate()
    second = await provider.generate()
    assert first.value != second.value
    assert first.state != second.state


async def test_read_the_first_message_of_a_shared_inbox(provider):
    # Any address can be taken, so this binds a session of our own to it.
    shared = await provider.generate(GenerateOptions(local="test", domain="getsomail.com"))
    assert shared.value == "test@getsomail.com"
    rows = await provider.list(shared)
    if not rows:
        pytest.skip("test@getsomail.com is empty right now; body-read path NOT tested")
    message = await provider.get(shared, rows[0].id)
    assert message.text
