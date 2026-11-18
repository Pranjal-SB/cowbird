"""Hits real tempr.email. Skipped by default; this is the canary.

The delivery half re-runs the Task 7 live capture end to end: open an alias,
hold the SSE watch, send a JoltMx test mail, and require the push frame to
name the message. A failure here is not a regression in this repo — tempr
changed, JoltMx's queue is backed up, or the runner's egress is blocked.
"""

from __future__ import annotations

import asyncio

import pytest
from cowbird.provider import GenerateOptions
from cowbird.transport import Transport
from cowbird_tempr import Tempr

pytestmark = pytest.mark.live

JOLTMX_SENDS = "https://testemailsender.com/api/tools/test-email/sends"


@pytest.fixture
async def provider():
    http = Transport("tempr")
    try:
        yield Tempr(http)
    finally:
        await http.aclose()


async def test_generate_and_empty_list_against_the_real_service(provider):
    address = await provider.generate(GenerateOptions())
    assert "@" in address.value
    assert address.state
    # list() is a bounded SSE collect: an empty inbox is [] after the idle
    # bound, never an exception.
    assert await provider.list(address) == []


async def test_a_pushed_message_arrives_and_can_be_read_and_deleted(provider):
    address = await provider.generate(GenerateOptions())

    send = await provider.http.json(
        "POST", JOLTMX_SENDS, json={"recipientEmail": address.value},
        headers={"referer": "https://testemailsender.com/"},
    )
    status_url = f"{JOLTMX_SENDS}/{send['id']}?token={send['token']}"

    async def deliver():
        for _ in range(36):
            status = await provider.http.json("GET", status_url)
            if status["status"] in ("Delivered", "Failed"):
                return status
            await asyncio.sleep(5)
        return {"status": "Queued"}

    # Watch and delivery-poll race: the watch must see the mail JoltMx pushes.
    watch_task = asyncio.create_task(
        provider.watch(address, poll=10.0).__anext__()
    )
    status = await deliver()
    try:
        message = await asyncio.wait_for(watch_task, timeout=120.0)
    except TimeoutError:
        pytest.fail(f"no push frame within 120s (send status: {status})")
    assert "joltmx" in message.sender.lower()
    assert message.text

    rows = await provider.list(address)
    row_ids = {r.id for r in rows}
    assert row_ids
    assert message.id in row_ids

    await provider.delete(address, message.id)
    remaining = {r.id for r in await provider.list(address)}
    assert message.id not in remaining
