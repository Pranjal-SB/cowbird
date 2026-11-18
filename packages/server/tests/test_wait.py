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


async def backtracking_body(self, address, id):
    return Message(id=id, sender="s@x.test", subject="hi", received_at=None, html="",
                   text="a" * 30 + "!")


def test_a_pattern_is_ignored_not_compiled(client_for, auth):
    # The server used to compile any client regex: "(" was a 500.
    with client_for(provider_class("fake", list=one_row, get=with_code)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        resp = client.get("/v1/inboxes/a@fake.test/wait?pattern=(&timeout=1", headers=auth)
    assert resp.status_code == 200


def test_a_backtracking_pattern_cannot_stall_the_server(client_for, auth):
    # (a+)+$ against 30 a's and a "!" blocked the event loop for over a minute.
    with client_for(provider_class("fake", list=one_row, get=backtracking_body)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        started = time.monotonic()
        resp = client.get(
            "/v1/inboxes/a@fake.test/wait",
            params={"pattern": "(a+)+$", "timeout": 1},
            headers=auth,
        )
    assert resp.status_code == 200
    assert time.monotonic() - started < 5


TWO_ROWS = [
    MessageRow(id="m1", sender="s@x.test", subject="signup", received_at=None),
    MessageRow(id="m2", sender="s@x.test", subject="login", received_at=None),
]


async def two_rows(self, address):
    return TWO_ROWS


async def two_codes(self, address, id):
    text = {"m1": "signup code 111111", "m2": "login code 222222"}[id]
    return Message(id=id, sender="s@x.test", subject="x", received_at=None, html="", text=text)


def test_wait_skips_ids_the_client_has_seen(client_for, auth):
    with client_for(provider_class("fake", list=two_rows, get=two_codes)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        url = "/v1/inboxes/a@fake.test/wait?otp=true&timeout=2"
        first = client.get(url, headers=auth).json()["data"]
        second = client.get(f"{url}&skip=m1", headers=auth).json()["data"]
    assert (first["otp"], second["otp"]) == ("111111", "222222")


def test_skip_tolerates_blanks(client_for, auth):
    with client_for(provider_class("fake", list=two_rows, get=two_codes)) as client:
        client.post("/v1/inboxes", headers=auth, json={})
        url = "/v1/inboxes/a@fake.test/wait?otp=true&timeout=2&skip=m1,,%20"
        assert client.get(url, headers=auth).json()["data"]["otp"] == "222222"
