"""Hits real zenvex.dev through a real solver. Skipped by default; this is the canary."""

import os

import pytest
from cowbird.models import Kind
from cowbird.provider import GenerateOptions
from cowbird.solver import Solver
from cowbird.transport import Transport
from cowbird_zenvex import Zenvex

pytestmark = [
    pytest.mark.live,
    pytest.mark.solver,
    pytest.mark.skipif(not os.environ.get("COWBIRD_SOLVER_URL"), reason="no COWBIRD_SOLVER_URL"),
]


class CountingSolver(Solver):
    solves = 0

    async def turnstile(self, url, sitekey, **kw):
        type(self).solves += 1
        return await super().turnstile(url, sitekey, **kw)


async def test_one_solve_reads_two_fresh_inboxes():
    solver = CountingSolver(os.environ["COWBIRD_SOLVER_URL"])
    http = Transport("zenvex", max_concurrency=Zenvex.caps.max_concurrency, solver=solver)
    try:
        provider = Zenvex(http)
        first = await provider.generate()
        second = await provider.generate(GenerateOptions(kind=Kind.EDU))
        assert second.value.endswith(".edu.pl")
        assert await provider.list(first) == []
        assert await provider.list(second) == []
    finally:
        await http.aclose()
    assert CountingSolver.solves == 1
