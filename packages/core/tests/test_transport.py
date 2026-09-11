import asyncio

import pytest
from cowbird.errors import CloudflareChallenge, ProviderDown, RateLimited, SchemaDrift
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
    t = transport_with(
        FakeResponse(status_code=403, text="<title>Just a moment...</title>")
    )
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
