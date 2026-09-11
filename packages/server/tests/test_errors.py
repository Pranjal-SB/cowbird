from __future__ import annotations

import pytest
from cowbird.errors import (
    AddressExpired,
    CloudflareChallenge,
    MessageLocked,
    NoProviderAvailable,
    NotSupported,
    ProviderDown,
    RateLimited,
    SchemaDrift,
)
from cowbird.models import Address
from cowbird.testing import build_pool, provider_class, raising
from cowbird_server import create_app
from cowbird_server.config import get_settings
from cowbird_server.errors import status_for
from cowbird_server.store import MemoryStore
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "exc,expected",
    [
        (NoProviderAvailable(tried=["fake"]), 503),
        (ProviderDown("boom"), 502),
        (CloudflareChallenge("challenge"), 502),
        (SchemaDrift("fake", expected="a", got="b"), 502),
        (RateLimited("slow down"), 429),
        (MessageLocked("paywalled"), 423),
        (NotSupported("no custom local"), 422),
        (AddressExpired("gone"), 410),
    ],
)
def test_every_library_error_has_a_deliberate_status(exc, expected):
    assert status_for(exc)[0] == expected


def test_the_client_message_never_carries_the_upstream_payload():
    # SchemaDrift stringifies to the shape it got from the backend. That is
    # right for a quarantine issue and wrong for an API response: it hands a
    # caller a slice of a third party's response body.
    drift = SchemaDrift("fake", expected="hydra:member", got={"secret": "leaked"})
    _status, message = status_for(drift)
    assert "leaked" not in message
    assert "hydra:member" not in message


def test_a_down_backend_is_a_502_in_an_envelope(client_for, auth):
    with client_for(provider_class("fake", list=raising(ProviderDown("boom")))) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        response = client.get("/v1/inboxes/a@fake.test/messages", headers=auth)
    assert response.status_code == 502
    assert response.json() == {
        "success": False,
        "data": None,
        "error": "upstream provider error",
    }


def test_nothing_satisfying_the_request_is_a_503(client, auth):
    response = client.post("/v1/inboxes", headers=auth, json={"kind": "gmail-alias"})
    assert response.status_code == 503


def test_a_validation_error_is_an_envelope_and_never_echoes_the_body(client, auth):
    # timeout is constrained to >= 1; -5 trips FastAPI's validation before the
    # route ever runs.
    response = client.get("/v1/inboxes/a@fake.test/wait?timeout=-5", headers=auth)
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert "detail" not in body
    assert "-5" not in body["error"]


def test_an_unmatched_route_is_a_404_envelope_not_starlettes_default(client, auth):
    # Starlette's router raises the base starlette.exceptions.HTTPException
    # for a 404, which a handler registered on fastapi.HTTPException (a
    # subclass) never sees.
    response = client.get("/v1/nope", headers=auth)
    assert response.status_code == 404
    assert response.json()["success"] is False
    assert "detail" not in response.json()


def test_a_wrong_method_is_a_405_envelope(client, auth):
    response = client.patch("/v1/providers", headers=auth)
    assert response.status_code == 405
    assert response.json()["success"] is False
    assert "detail" not in response.json()


def test_an_unhandled_exception_is_a_500_envelope_without_the_raw_message(
    monkeypatch, tmp_path, auth
):
    # Starlette's ServerErrorMiddleware always re-raises after handing the
    # response to the ASGI send callable -- for a real server that's just
    # for logging, but TestClient's default raise_server_exceptions=True
    # re-raises it into the test too. raise_server_exceptions=False is what a
    # real deployment sees: the enveloped 500, nothing more.
    monkeypatch.setenv("API_KEYS", "test-key")
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()
    pool = build_pool(provider_class("fake", list=raising(RuntimeError("db password: hunter2"))))
    with TestClient(create_app(pool=pool), raise_server_exceptions=False) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        response = client.get("/v1/inboxes/a@fake.test/messages", headers=auth)
    get_settings.cache_clear()

    assert response.status_code == 500
    body = response.json()
    assert body == {"success": False, "data": None, "error": "internal server error"}
    assert "hunter2" not in response.text


async def test_a_stored_address_for_an_uninstalled_provider_is_a_503_not_a_500(
    auth, tmp_path, monkeypatch
):
    # An address can outlive the provider that issued it: the registry the app
    # is built with may not include that name any more. InboxService.inbox()
    # rebuilds the provider by name on every call, so Registry.get raises
    # NoProviderAvailable — this pins that it comes back through the real
    # CowbirdError handler as a 503 in an envelope, not an unhandled 500/KeyError.
    monkeypatch.setenv("API_KEYS", "test-key")
    monkeypatch.setenv("COWBIRD_HEALTH_PATH", str(tmp_path / "health.json"))
    get_settings.cache_clear()

    store = MemoryStore()
    await store.put(Address(value="a@gone.test", provider="gone"))

    with TestClient(create_app(pool=build_pool(provider_class("fake")), store=store)) as client:
        response = client.get("/v1/inboxes/a@gone.test/messages", headers=auth)
    get_settings.cache_clear()

    assert response.status_code == 503


def test_message_gone_maps_to_410_with_a_fixed_message():
    from cowbird.errors import MessageGone
    from cowbird_server.errors import status_for

    assert status_for(MessageGone("tempmailorg: 410 on 6aa3")) == (
        410,
        "that message is no longer available",
    )
