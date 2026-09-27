"""Hits real etempmail.com through a real solver. Skipped by default; this is the canary."""

import os

import pytest
from cowbird.models import Address
from cowbird.solver import Solver
from cowbird.transport import Transport
from cowbird_etempmail import ETempMail

pytestmark = [
    pytest.mark.live,
    pytest.mark.solver,
    pytest.mark.skipif(not os.environ.get("COWBIRD_SOLVER_URL"), reason="no COWBIRD_SOLVER_URL"),
]


def transport():
    return Transport(
        "etempmail",
        max_concurrency=ETempMail.caps.max_concurrency,
        fresh_session=ETempMail.caps.fresh_session,
        solver=Solver.from_env(),
    )


async def test_two_generates_differ_and_resume_from_state_alone_on_a_fresh_transport():
    # The inbox is keyed on a session cookie, so a shared jar would hand the
    # second generate the first address.
    first = transport()
    try:
        provider = ETempMail(first)
        address = await provider.generate()
        other = await provider.generate()
    finally:
        await first.aclose()
    assert address.value != other.value
    assert address.state

    # A separate process carrying only the value and the state.
    second = transport()
    try:
        resumed = Address(address.value, "etempmail", state=address.state)
        assert await ETempMail(second).list(resumed) == []
    finally:
        await second.aclose()
