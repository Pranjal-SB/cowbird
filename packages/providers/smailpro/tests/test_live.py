"""Hits real smailpro.com through a real solver. Skipped by default; this is the canary."""

import os

import pytest
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.solver import Solver
from cowbird.transport import Transport
from cowbird_smailpro import SmailPro

pytestmark = [
    pytest.mark.live,
    pytest.mark.solver,
    pytest.mark.skipif(not os.environ.get("COWBIRD_SOLVER_URL"), reason="no COWBIRD_SOLVER_URL"),
]


def transport():
    return Transport(
        "smailpro", max_concurrency=SmailPro.caps.max_concurrency, solver=Solver.from_env()
    )


@pytest.mark.parametrize("kind", [Kind.GMAIL_ALIAS, Kind.OUTLOOK_ALIAS])
async def test_generate_then_list_from_state_alone_on_a_fresh_transport(kind):
    first = transport()
    try:
        address = await SmailPro(first).generate(GenerateOptions(kind=kind))
    finally:
        await first.aclose()
    assert address.state

    # A separate process carrying only the value and the state.
    second = transport()
    try:
        resumed = Address(address.value, "smailpro", state=address.state)
        assert await SmailPro(second).list(resumed) == []
    finally:
        await second.aclose()
