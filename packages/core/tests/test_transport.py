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
