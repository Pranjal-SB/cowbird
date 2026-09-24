import asyncio

import pytest
from cowbird.errors import (
    CloudflareChallenge,
    ProviderDown,
    RateLimited,
    SchemaDrift,
    SolverUnavailable,
)
from cowbird.testing import FakeSolver
from cowbird.transport import Transport


class FakeResponse:
    def __init__(self, status_code=200, text='{"ok": true}', headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeSession:
    """Stands in for curl_cffi's AsyncSession. Returns queued responses in order."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = 0

    async def request(self, method, url, **kw):
        self.calls += 1
        item = self.queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


def transport_with(*responses, **kw) -> Transport:
    t = Transport("fake", **kw)
    t._session = FakeSession(*responses)
    return t


async def test_json_returns_parsed_body():
    t = transport_with(FakeResponse(text='{"email": "a@b.c"}'))
    assert await t.json("GET", "https://x.test") == {"email": "a@b.c"}


async def test_non_json_body_raises_schema_drift_not_a_value_error():
    t = transport_with(FakeResponse(text="<html>nope</html>"))
    with pytest.raises(SchemaDrift):
        await t.json("GET", "https://x.test")


async def test_cloudflare_interstitial_is_recognised():
    t = transport_with(FakeResponse(status_code=403, text="<title>Just a moment...</title>"))
    with pytest.raises(CloudflareChallenge):
        await t.json("GET", "https://x.test")


async def test_429_raises_rate_limited_without_retrying():
    t = transport_with(FakeResponse(status_code=429, text="slow down"), retries=2)
    with pytest.raises(RateLimited):
        await t.json("GET", "https://x.test")
    assert t._session.calls == 1


async def test_5xx_is_retried_then_succeeds():
    t = transport_with(
        FakeResponse(status_code=502, text="bad gateway"),
        FakeResponse(text='{"ok": 1}'),
        retries=2,
    )
    assert await t.json("GET", "https://x.test") == {"ok": 1}
    assert t._session.calls == 2


async def test_5xx_past_the_retry_budget_raises_provider_down():
    t = transport_with(
        FakeResponse(status_code=502, text="bad"),
        FakeResponse(status_code=502, text="bad"),
        retries=1,
    )
    with pytest.raises(ProviderDown):
        await t.json("GET", "https://x.test")


async def test_send_returns_the_raw_response():
    t = transport_with(FakeResponse(status_code=404, text="{}"))
    resp = await t.send("DELETE", "https://x.test")
    assert resp.status_code == 404


async def test_send_merges_accept_json_but_a_caller_accept_wins():
    seen = {}

    class RecordingSession(FakeSession):
        async def request(self, method, url, **kw):
            seen["headers"] = kw.get("headers")
            return await super().request(method, url, **kw)

    t = transport_with(FakeResponse())
    t._session = RecordingSession(FakeResponse())
    await t.send("GET", "https://x.test")
    assert seen["headers"]["Accept"] == "application/json"

    t._session = RecordingSession(FakeResponse())
    await t.send("GET", "https://x.test", headers={"Accept": "text/xml"})
    assert seen["headers"]["Accept"] == "text/xml"


async def test_send_goes_through_the_concurrency_gate():
    class ConcurrencyTrackingSession(FakeSession):
        def __init__(self):
            super().__init__(FakeResponse(), FakeResponse())
            self.active = 0
            self.peak = 0

        async def request(self, method, url, **kw):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return self.queued.pop(0)

    t = Transport("fake", max_concurrency=1)
    t._session = ConcurrencyTrackingSession()
    await asyncio.gather(
        t.send("GET", "https://x.test"),
        t.send("GET", "https://x.test"),
    )
    assert t._session.peak == 1


class ClosingSession(FakeSession):
    closed = False

    async def close(self):
        self.closed = True


async def test_fresh_session_runs_each_request_on_its_own_closed_session():
    # A backend that keys the inbox on a session cookie hands the same address
    # to every generate() on a shared jar. A fresh session per request means no
    # cookie set by one call is ever sent by another.
    made = []

    def new_session():
        session = ClosingSession(FakeResponse())
        made.append(session)
        return session

    t = Transport("fake", fresh_session=True)
    t._new_session = new_session
    await t.json("GET", "https://x.test/a")
    await t.json("GET", "https://x.test/b")
    assert len(made) == 2
    assert all(session.closed for session in made)
    assert t._session is None


async def test_default_transport_reuses_one_session():
    t = transport_with(FakeResponse(), FakeResponse())
    first = t._session
    await t.json("GET", "https://x.test/a")
    await t.json("GET", "https://x.test/b")
    assert t._session is first
    assert first.calls == 2


# --------------------------------------------------------------------------
# Cloudflare clearance through a solver
# --------------------------------------------------------------------------

CHALLENGE = {"status_code": 403, "text": "<title>Just a moment...</title>"}


class RecordingSession(FakeSession):
    def __init__(self, *responses):
        super().__init__(*responses)
        self.kwargs: list[dict] = []

    async def request(self, method, url, **kw):
        # Yield like a real network call, so concurrent requests interleave.
        await asyncio.sleep(0)
        self.kwargs.append(kw)
        return await super().request(method, url, **kw)


def solved_transport(*responses, solver=None, **kw):
    t = Transport("fake", solver=solver if solver is not None else FakeSolver(), **kw)
    t._session = RecordingSession(*responses)
    return t


async def test_a_challenge_is_solved_once_and_retried_with_cookie_and_user_agent():
    solver = FakeSolver()
    t = solved_transport(FakeResponse(**CHALLENGE), FakeResponse(), solver=solver)
    assert await t.json("POST", "https://x.test/api/thing") == {"ok": True}
    assert solver.calls == [("clearance", "https://x.test/", None)]
    retry = t._session.kwargs[1]
    assert retry["cookies"]["cf_clearance"] == "fake-clearance"
    assert retry["headers"]["User-Agent"] == "FakeSolver/1.0"


async def test_a_second_challenge_after_solving_raises_and_forgets_the_clearance():
    solver = FakeSolver()
    t = solved_transport(FakeResponse(**CHALLENGE), FakeResponse(**CHALLENGE), solver=solver)
    with pytest.raises(CloudflareChallenge):
        await t.json("GET", "https://x.test/")
    assert len(solver.calls) == 1
    assert t._clearances == {}


async def test_a_cached_clearance_is_sent_before_any_challenge():
    solver = FakeSolver()
    t = solved_transport(FakeResponse(**CHALLENGE), FakeResponse(), FakeResponse(), solver=solver)
    await t.json("GET", "https://X.test/a")
    await t.json("GET", "https://x.test/b")
    assert len(solver.calls) == 1
    assert t._session.kwargs[2]["cookies"]["cf_clearance"] == "fake-clearance"


async def test_the_callers_cookies_survive_and_are_not_mutated():
    t = solved_transport(FakeResponse(**CHALLENGE), FakeResponse())
    mine = {"JSESSIONID": "abc"}
    await t.json("GET", "https://x.test/", cookies=mine)
    assert t._session.kwargs[1]["cookies"] == {
        "JSESSIONID": "abc",
        "cf_clearance": "fake-clearance",
    }
    assert mine == {"JSESSIONID": "abc"}


async def test_without_a_solver_a_challenge_raises_as_before():
    t = transport_with(FakeResponse(**CHALLENGE))
    assert t.solver is None
    with pytest.raises(CloudflareChallenge):
        await t.json("GET", "https://x.test/")


async def test_a_failing_solver_surfaces_as_solver_unavailable():
    solver = FakeSolver(clearance=SolverUnavailable("down"))
    t = solved_transport(FakeResponse(**CHALLENGE), solver=solver)
    with pytest.raises(SolverUnavailable):
        await t.json("GET", "https://x.test/")


async def test_concurrent_challenged_requests_cost_one_solve():
    solver = FakeSolver()
    t = solved_transport(
        FakeResponse(**CHALLENGE),
        FakeResponse(**CHALLENGE),
        FakeResponse(),
        FakeResponse(),
        solver=solver,
    )
    await asyncio.gather(t.json("GET", "https://x.test/a"), t.json("GET", "https://x.test/b"))
    assert len(solver.calls) == 1


async def test_a_fresh_session_transport_still_carries_the_clearance():
    sessions = []

    t = Transport("fake", solver=FakeSolver(), fresh_session=True)

    def new_session():
        session = RecordingSession(
            *([FakeResponse(**CHALLENGE)] if not sessions else [FakeResponse()])
        )
        sessions.append(session)
        return session

    t._new_session = new_session
    await t.json("GET", "https://x.test/")
    assert sessions[1].kwargs[0]["cookies"]["cf_clearance"] == "fake-clearance"


def test_a_proxied_transport_hands_its_proxy_to_the_solver():
    solver = FakeSolver()
    Transport("fake", solver=solver, proxy="http://proxy.test:8080")
    assert solver.proxy == "http://proxy.test:8080"
