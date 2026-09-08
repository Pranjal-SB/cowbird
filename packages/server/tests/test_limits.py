from __future__ import annotations

from cowbird.testing import provider_class


def test_the_rate_limit_returns_429_in_an_envelope(client_for, auth):
    with client_for(provider_class("fake"), RATE_LIMIT="2/minute") as client:
        responses = [client.get("/v1/providers", headers=auth) for _ in range(4)]
    codes = [r.status_code for r in responses]
    assert codes[:2] == [200, 200]
    assert 429 in codes[2:]
    limited = next(r for r in responses if r.status_code == 429)
    assert limited.json() == {"success": False, "data": None, "error": "rate limit exceeded"}


def test_rotating_bogus_api_keys_cannot_evade_the_rate_limit(client_for):
    # Before the fix, _rate_key bucketed on the (unvalidated, attacker-
    # controlled) x-api-key header alone: a fresh bogus key every request
    # meant a fresh bucket every request, so this ran 401 forever and never
    # tripped the limiter.
    with client_for(provider_class("fake"), RATE_LIMIT="2/minute") as client:
        responses = [
            client.get("/v1/providers", headers={"x-api-key": f"bogus-{i}"}) for i in range(8)
        ]
    codes = [r.status_code for r in responses]
    assert codes[:2] == [401, 401]
    assert 429 in codes[2:], f"never rate limited: {codes}"


def test_health_is_not_rate_limited(client_for):
    # A load balancer probes /health far more often than any client calls the
    # API. Rate limiting the probe takes the instance out of rotation for being
    # alive too enthusiastically.
    with client_for(provider_class("fake"), RATE_LIMIT="2/minute") as client:
        codes = [client.get("/health").status_code for _ in range(6)]
    assert codes == [200] * 6


def test_cors_is_off_unless_origins_are_configured(client_for):
    with client_for(provider_class("fake")) as client:
        response = client.get("/health", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in response.headers


def test_a_configured_origin_is_allowed(client_for):
    with client_for(provider_class("fake"), ALLOWED_ORIGINS="https://app.test") as client:
        response = client.get("/health", headers={"Origin": "https://app.test"})
    assert response.headers["access-control-allow-origin"] == "https://app.test"
