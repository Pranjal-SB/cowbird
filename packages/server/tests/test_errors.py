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
from cowbird_server.errors import status_for
from cowbird_server.service import InboxService
from cowbird_server.store import MemoryStore


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


async def test_inbox_service_raises_no_provider_available_for_an_uninstalled_provider():
    # An address can outlive the provider that issued it: the registry it was
    # built against may not include that name any more. InboxService.inbox()
    # rebuilds the provider by name on every call, so a stored address for a
    # provider that isn't registered must surface as NoProviderAvailable (and
    # therefore 503 via status_for), not a raw KeyError from the registry.
    store = MemoryStore()
    await store.put(Address(value="a@gone.test", provider="gone"))
    service = InboxService(build_pool(provider_class("fake")), store)

    with pytest.raises(NoProviderAvailable):
        await service.inbox("a@gone.test")
