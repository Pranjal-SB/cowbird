"""Proves the solver wiring against a real solver and a real Cloudflare wall.

Needs COWBIRD_SOLVER_URL and `-m live`. It also proves the egress assumption
on the machine it runs on: a clearance only works if this process and the
solver leave through the same public IP.
"""

import os

import pytest
from cowbird.errors import CloudflareChallenge
from cowbird.solver import Solver
from cowbird.transport import Transport

pytestmark = [
    pytest.mark.live,
    pytest.mark.solver,
    pytest.mark.skipif(not os.environ.get("COWBIRD_SOLVER_URL"), reason="no COWBIRD_SOLVER_URL"),
]


IUAM_SITE = "https://10minutemail.com/"


async def test_a_challenged_site_is_reached_through_the_solver():
    # 10minutemail serves Cloudflare's interstitial to some egress IPs and not
    # others. Without a challenge there is nothing to prove, and passing would
    # claim a solve that never happened.
    bare = Transport("probe", retries=0)
    try:
        await bare.text("GET", IUAM_SITE)
        pytest.skip(f"{IUAM_SITE} is not challenging this egress right now")
    except CloudflareChallenge:
        pass
    finally:
        await bare.aclose()

    http = Transport("probe", solver=Solver.from_env())
    try:
        page = await http.text("GET", IUAM_SITE)
    finally:
        await http.aclose()
    assert http._clearances, "reached the site without solving"
    assert "10minutemail" in page.lower()


async def test_the_solver_hands_back_a_turnstile_token():
    token = await Solver.from_env().turnstile(
        "https://smailpro.com/temporary-email", "0x4AAAAAAABIS_gEec2IwOhI"
    )
    assert len(token) > 20
