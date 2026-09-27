"""Hits real vanishinbox.com through a real solver. Skipped by default; this is the canary."""

import os

import pytest
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.solver import Solver
from cowbird.transport import Transport
from cowbird_vanishinbox import VanishInbox

pytestmark = [
    pytest.mark.live,
    pytest.mark.solver,
    pytest.mark.skipif(not os.environ.get("COWBIRD_SOLVER_URL"), reason="no COWBIRD_SOLVER_URL"),
]


def transport():
    return Transport(
        "vanishinbox", max_concurrency=VanishInbox.caps.max_concurrency, solver=Solver.from_env()
    )


async def test_generate_then_list_from_state_alone_on_a_fresh_transport():
    first = transport()
    try:
        provider = VanishInbox(first)
        address = await provider.generate()
        edu = await provider.generate(GenerateOptions(kind=Kind.EDU))
    finally:
        await first.aclose()
    assert address.state and address.value != edu.value

    # A separate process carrying only the value and the state: no solve.
    second = transport()
    second.solver = None
    try:
        for addr in (address, edu):
            resumed = Address(addr.value, "vanishinbox", state=addr.state)
            assert await VanishInbox(second).list(resumed) == []
    finally:
        await second.aclose()
