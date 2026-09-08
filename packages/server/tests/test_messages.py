from __future__ import annotations

from cowbird.errors import MessageLocked
from cowbird.models import Message
from cowbird.parsing import extract_links, html_to_text
from cowbird.testing import provider_class, raising

HTML = '<p>Your code is 123456</p><a href="https://example.test/verify">verify</a>'


async def one_message(self, address, id):
    return Message(
        id=id,
        sender="s@x.test",
        subject="hi",
        received_at=None,
        html=HTML,
        text=html_to_text(HTML),
        links=extract_links(HTML),
    )


def test_reading_a_message_returns_text_links_and_the_otp(client_for, auth):
    with client_for(provider_class("fake", get=one_message)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        data = client.get("/v1/inboxes/a@fake.test/messages/1", headers=auth).json()["data"]
    assert data["text"] == "Your code is 123456 verify"
    assert data["links"] == ["https://example.test/verify"]
    # The OTP is computed server-side because pulling it out of a mail body is
    # the single most-copied snippet in this domain.
    assert data["otp"] == "123456"
    assert data["html"] == HTML


def test_a_paywalled_message_is_a_423(client_for, auth):
    with client_for(provider_class("fake", get=raising(MessageLocked("premium")))) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        response = client.get("/v1/inboxes/a@fake.test/messages/1", headers=auth)
    assert response.status_code == 423


def test_deleting_on_a_provider_that_cannot_delete_is_a_422(client, auth):
    # FakeProvider declares delete=False and inherits Provider.delete, which
    # raises NotSupported. A 422 says "this provider will never do that", which
    # is a different answer from the backend being broken.
    client.post("/v1/inboxes", headers=auth, json={})
    response = client.delete("/v1/inboxes/a@fake.test/messages/1", headers=auth)
    assert response.status_code == 422


def test_deleting_succeeds_when_the_provider_supports_it(client_for, auth):
    deleted = []

    async def delete(self, address, id):
        deleted.append(id)

    with client_for(provider_class("fake", delete=delete)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        response = client.delete("/v1/inboxes/a@fake.test/messages/1", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": {"deleted": "1"}, "error": None}
    assert deleted == ["1"]
