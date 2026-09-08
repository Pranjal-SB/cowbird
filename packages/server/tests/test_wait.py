from __future__ import annotations

import time

from cowbird.models import Message, MessageRow
from cowbird.testing import provider_class

ROW = MessageRow(id="1", sender="s@x.test", subject="hi", received_at=None)


async def one_row(self, address):
    return [ROW]


async def with_code(self, address, id):
    return Message(
        id=id,
        sender="s@x.test",
        subject="hi",
        received_at=None,
        html="",
        text="your code is 445566",
    )


def test_wait_returns_the_first_message(client_for, auth):
    with client_for(provider_class("fake", list=one_row, get=with_code)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        data = client.get("/v1/inboxes/a@fake.test/wait?timeout=5", headers=auth).json()["data"]
    assert data["id"] == "1"


def test_wait_with_otp_returns_the_code(client_for, auth):
    with client_for(provider_class("fake", list=one_row, get=with_code)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        data = client.get(
            "/v1/inboxes/a@fake.test/wait?timeout=5&otp=1", headers=auth
        ).json()["data"]
    assert data["otp"] == "445566"


def test_nothing_arriving_is_a_200_with_null_data_not_an_error(client, auth):
    # A quiet inbox is the normal case, not a failure. A 504 here would make
    # every polling client treat "no mail yet" as a broken backend.
    client.post("/v1/inboxes", headers=auth, json={})
    response = client.get("/v1/inboxes/a@fake.test/wait?timeout=1", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": None, "error": None}


def test_a_requested_timeout_above_the_cap_is_clamped(client_for, auth):
    # A client asking for 600 seconds gets wait_max. The hold has to stay under
    # the edge's subrequest limit, or the worker kills it mid-flight and the
    # caller sees a 5xx indistinguishable from a real failure.
    with client_for(provider_class("fake"), WAIT_MAX="2") as client:
        client.post("/v1/inboxes", headers=auth, json={})
        started = time.monotonic()
        response = client.get("/v1/inboxes/a@fake.test/wait?timeout=600", headers=auth)
        elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.json() == {"success": True, "data": None, "error": None}
    assert 1.5 < elapsed < 10, f"waited {elapsed:.1f}s; the cap was not applied"
