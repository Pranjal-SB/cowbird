"""Hits real xeramail.com through a real solver. Skipped by default; this is the canary."""

import os

import pytest
from cowbird.models import Address
from cowbird.solver import Solver
from cowbird.transport import Transport
from cowbird_xeramail import Xeramail

pytestmark = [
    pytest.mark.live,
    pytest.mark.solver,
    pytest.mark.skipif(not os.environ.get("COWBIRD_SOLVER_URL"), reason="no COWBIRD_SOLVER_URL"),
]


def transport():
    return Transport(
        "xeramail", max_concurrency=Xeramail.caps.max_concurrency, solver=Solver.from_env()
    )


async def test_generate_then_list_from_state_alone_on_a_fresh_transport():
    first = transport()
    try:
        address = await Xeramail(first).generate()
    finally:
        await first.aclose()
    assert address.state

    # A separate process carrying only the value and the secret.
    second = transport()
    try:
        resumed = Address(address.value, "xeramail", state=address.state)
        assert await Xeramail(second).list(resumed) == []
    finally:
        await second.aclose()
