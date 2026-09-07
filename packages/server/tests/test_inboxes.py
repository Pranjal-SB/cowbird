from __future__ import annotations

from datetime import UTC, datetime

from cowbird.models import MessageRow
from cowbird.testing import provider_class


def test_creating_an_inbox_returns_the_address_and_its_provider(client, auth):
    response = client.post("/v1/inboxes", headers=auth, json={})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["address"] == "a@fake.test"
    assert data["provider"] == "fake"


def test_creating_an_inbox_never_returns_the_provider_state(client, auth):
    # state is a credential. It is held server-side precisely so it does not
    # travel, and a response that carries it defeats the whole store.
    response = client.post("/v1/inboxes", headers=auth, json={})
    assert "state" not in response.json()["data"]


def test_an_empty_inbox_lists_as_an_empty_array(client, auth):
    client.post("/v1/inboxes", headers=auth, json={})
    response = client.get("/v1/inboxes/a@fake.test/messages", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": [], "error": None}


def test_listing_an_address_this_server_never_issued_is_a_404(client, auth):
    response = client.get("/v1/inboxes/stranger@fake.test/messages", headers=auth)
    assert response.status_code == 404
    assert response.json()["success"] is False


def test_messages_are_listed_with_their_headers(client_for, auth):
    when = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    rows = [MessageRow(id="1", sender="s@x.test", subject="hi", received_at=when, locked=True)]

    async def one_page(self, address):
        return rows

    with client_for(provider_class("fake", list=one_page)) as c:
        c.post("/v1/inboxes", headers=auth, json={})
        data = c.get("/v1/inboxes/a@fake.test/messages", headers=auth).json()["data"]
    assert data == [
        {
            "id": "1",
            "sender": "s@x.test",
            "subject": "hi",
            "received_at": "2026-09-07T12:00:00+00:00",
            "locked": True,
        }
    ]


def test_a_capability_request_is_passed_through_to_routing(client, auth):
    # `kind` must reach Pool.candidates, or every capability filter the library
    # offers is silently unavailable over HTTP. The fake provider serves
    # own-domain only, so nothing satisfies gmail-alias and Pool.acquire raises
    # NoProviderAvailable.
    response = client.post("/v1/inboxes", headers=auth, json={"kind": "gmail-alias"})
    assert response.status_code == 503
