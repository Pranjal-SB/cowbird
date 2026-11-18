"""Hits real 10minutemail.net. Skipped by default; this is the canary.

The welcome mail the service issues on generate proves generate → list → get
end to end without an external sender.
"""

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_tenminutemailnet import TenMinuteMailNet

pytestmark = pytest.mark.live


@pytest.fixture
async def provider():
    http = Transport("tenminutemailnet", fresh_session=True)
    try:
        yield TenMinuteMailNet(http)
    finally:
        await http.aclose()


async def test_generate_list_and_read_the_welcome_mail(provider):
    address = await provider.generate(GenerateOptions())
    assert address.value.endswith("@zefvo.com")
    assert address.state
    rows = await provider.list(address)
    assert rows, "the welcome mail should be present immediately after generate"
    message = await provider.get(address, rows[0].id)
    assert message.text
    second = await provider.generate(GenerateOptions())
    assert second.value != address.value, "cookie-keyed inboxes must not collide"
