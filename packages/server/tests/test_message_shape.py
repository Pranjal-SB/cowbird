from __future__ import annotations

from cowbird.models import Message, MessageRow
from cowbird.testing import provider_class
from cowbird_server.messages import message_dict

HTML = "<p>Your code is 654321</p>"
TEXT = "Your code is 654321"
ROW = MessageRow(id="1", sender="s@x.test", subject="hi", received_at=None)

COMMON_FIELDS = {"id", "sender", "subject", "received_at", "html", "text", "links", "otp"}


async def list_one(self, address):
    return [ROW]


async def get_one(self, address, id):
    return Message(id=id, sender="s@x.test", subject="hi", received_at=None, html=HTML, text=TEXT)


def test_get_message_and_wait_agree_on_shape(client_for, auth):
    with client_for(provider_class("fake", list=list_one, get=get_one)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        fetched = client.get("/v1/inboxes/a@fake.test/messages/1", headers=auth).json()["data"]
        waited = client.get("/v1/inboxes/a@fake.test/wait?timeout=5", headers=auth).json()["data"]

    assert fetched.keys() >= COMMON_FIELDS
    assert waited.keys() >= COMMON_FIELDS
    for field in COMMON_FIELDS - {"otp"}:
        assert fetched[field] == waited[field]


def test_the_webhook_payload_uses_the_same_message_shape():
    message = Message(
        id="1", sender="s@x.test", subject="hi", received_at=None, html=HTML, text=TEXT
    )
    assert set(message_dict(message, "654321").keys()) == COMMON_FIELDS
