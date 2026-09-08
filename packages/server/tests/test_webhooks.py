from __future__ import annotations

import hashlib
import hmac
import json
import socket

import pytest
from cowbird_server.webhooks import WebhookManager, validate_url

PUBLIC = "93.184.216.34"


class FakeSession:
    """Records every `post()` call; never touches a socket."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.calls: list[dict] = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return type("Resp", (), {"status_code": self.status_code})()


def _resolves_to(ip: str):
    def resolve(host, port, proto=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]

    return resolve


def test_a_public_url_is_accepted():
    assert validate_url("https://example.test/hook", False, _resolves_to(PUBLIC))


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0"]
)
def test_a_host_resolving_to_an_internal_address_is_refused(ip):
    # 169.254.169.254 is the cloud metadata endpoint. A hostname that resolves
    # there is the whole reason this guard exists: the check is on the resolved
    # address, not on whether the URL looks like an IP.
    with pytest.raises(ValueError, match="private"):
        validate_url("https://sneaky.test/hook", False, _resolves_to(ip))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x.test/", "ftp://x.test/"])
def test_only_http_and_https_are_accepted(url):
    with pytest.raises(ValueError, match="http"):
        validate_url(url, False, _resolves_to(PUBLIC))


def test_the_private_override_skips_resolution_entirely():
    # Self-hosters point hooks at a service on the same network. The override
    # is explicit and off by default.
    assert validate_url("http://localhost:9000/hook", True)


def test_registering_a_hook_at_an_internal_address_is_a_422(client, auth, monkeypatch):
    monkeypatch.setattr("cowbird_server.webhooks.socket.getaddrinfo", _resolves_to("127.0.0.1"))
    client.post("/v1/inboxes", headers=auth, json={})
    response = client.post(
        "/v1/webhooks",
        headers=auth,
        json={"address": "a@fake.test", "url": "http://internal.test/hook"},
    )
    assert response.status_code == 422


def test_a_registered_hook_is_listed_and_can_be_cancelled(client, auth, monkeypatch):
    # Resolution is stubbed so the offline suite never makes a DNS query.
    monkeypatch.setattr("cowbird_server.webhooks.socket.getaddrinfo", _resolves_to(PUBLIC))
    client.post("/v1/inboxes", headers=auth, json={})
    created = client.post(
        "/v1/webhooks",
        headers=auth,
        json={"address": "a@fake.test", "url": "https://example.test/hook", "timeout": 1},
    )
    assert created.status_code == 200
    hook_id = created.json()["data"]["id"]
    assert [h["id"] for h in client.get("/v1/webhooks", headers=auth).json()["data"]] == [hook_id]
    assert client.delete(f"/v1/webhooks/{hook_id}", headers=auth).json()["data"] == {
        "cancelled": True
    }


def test_a_hook_never_echoes_the_signing_secret(client, auth, monkeypatch):
    monkeypatch.setattr("cowbird_server.webhooks.socket.getaddrinfo", _resolves_to(PUBLIC))
    client.post("/v1/inboxes", headers=auth, json={})
    created = client.post(
        "/v1/webhooks",
        headers=auth,
        json={"address": "a@fake.test", "url": "https://example.test/hook", "timeout": 1},
    )
    listed = client.get("/v1/webhooks", headers=auth)
    for body in (created.text, listed.text):
        assert "secret" not in body.lower()


async def test_delivery_disables_redirects_and_signs_the_body():
    # allow_redirects=False is the second half of the SSRF defence: validate_url
    # checks the target at registration time, but a permitted target can still
    # answer with a 302 to an internal address at delivery time. Without this
    # flag the redirect would be followed.
    session = FakeSession()
    secret = "s3cr3t"
    manager = WebhookManager(service=None, allow_private=True, secret=secret, session=session)

    await manager._deliver("https://example.test/hook", {"event": "message"})

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["allow_redirects"] is False

    body = json.dumps({"event": "message"}).encode()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert call["headers"]["x-cowbird-signature"] == expected


async def test_delivery_without_a_secret_sends_no_signature_header():
    session = FakeSession()
    manager = WebhookManager(service=None, allow_private=True, secret=None, session=session)

    await manager._deliver("https://example.test/hook", {"event": "message"})

    assert "x-cowbird-signature" not in session.calls[0]["headers"]
