import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, SchemaDrift
from cowbird.testing import FakeTransport, Responses
from cowbird_anonymmail import AnonymMail

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes, status=200):
    return FakeTransport("anonymmail", routes, status)


DEFAULT_ROUTES = {
    "/": "",
    "/api/csrf": {"name": "csrf_test_name", "hash": "abc123"},
    "/api/getDomains": [
        {"domain": "openstudy.edu.pl", "left": 246},
        {"domain": "melorvian.com", "left": 246},
    ],
    "/api/create": {"success": True, "email": "cb1@openstudy.edu.pl"},
    "/api/get": {
        "cb1@openstudy.edu.pl": {
            "created_at": "15/11/2026 15:56:55",
            "emails": [
                {
                    "token": "tok1",
                    "subject": "Confirm",
                    "from": "noreply@example.test",
                    "date": "15/11/2026 15:57:20",
                    "body": "SGVsbG8=",  # "Hello"
                }
            ],
        }
    },
}


class TestAnonymMailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return AnonymMail(fake(dict(DEFAULT_ROUTES)))


async def test_list_parses_rows_with_decoded_body():
    http = fake(dict(DEFAULT_ROUTES))
    provider = AnonymMail(http)
    address = await provider.generate()
    rows = await provider.list(address)
    assert rows[0].id == "tok1"
    assert rows[0].sender == "noreply@example.test"


async def test_get_decodes_the_base64_body():
    provider = AnonymMail(fake(dict(DEFAULT_ROUTES)))
    address = await provider.generate()
    message = await provider.get(address, "tok1")
    assert message.text == "Hello"


async def test_a_garbled_body_passes_through_undecoded():
    provider = AnonymMail(fake(dict(DEFAULT_ROUTES)))
    address = await provider.generate()
    message = await provider.get(address, "tok1")
    assert message.text == "Hello"


async def test_an_inbox_missing_from_the_response_is_schema_drift():
    http = fake(dict(DEFAULT_ROUTES))
    provider = AnonymMail(http)
    address = await provider.generate()
    http.routes["/api/get"] = {}
    with pytest.raises(SchemaDrift):
        await provider.list(address)


async def test_a_message_already_handed_out_is_served_from_the_cache():
    # The backend returns each message exactly once: the second /api/get is
    # empty. get() must still work off what list() saw.
    from cowbird.testing import FakeTransport as FT

    inbox = {
        "cb1@openstudy.edu.pl": {
            "created_at": "15/11/2026 15:56:55",
            "emails": [
                {"token": "tok1", "subject": "Confirm", "from": "noreply@example.test",
                 "date": "15/11/2026 15:57:20", "body": "SGVsbG8="}
            ],
        }
    }
    http = FT("anonymmail", {**DEFAULT_ROUTES, "/api/get": Responses(
        inbox,
        {"cb1@openstudy.edu.pl": {"created_at": "15/11/2026 15:56:55", "emails": []}},
    )})
    provider = AnonymMail(http)
    address = await provider.generate()
    rows = await provider.list(address)
    assert rows[0].id == "tok1"
    assert await provider.list(address) == []  # consumed
    message = await provider.get(address, "tok1")  # but still readable
    assert message.text == "Hello"


async def test_get_of_an_unknown_id_raises_message_gone():
    provider = AnonymMail(fake(dict(DEFAULT_ROUTES)))
    address = await provider.generate()
    with pytest.raises(MessageGone):
        await provider.get(address, "nope")


async def test_a_padding_stripped_body_still_decodes():
    # The API strips `=` padding and wraps the base64 in CRLF; the real
    # Mailman confirmation is what exposed this (get() returned raw base64).
    import base64 as b64

    from cowbird.testing import FakeTransport as FT

    body = b64.b64encode(b"Hello padded world").decode().rstrip("=")
    wrapped = "\r\n".join(body[i : i + 60] for i in range(0, len(body), 60))
    routes = dict(DEFAULT_ROUTES)
    routes["/api/get"] = {
        "cb1@openstudy.edu.pl": {
            "emails": [{"token": "tok1", "subject": "s", "from": "f",
                        "date": "x", "body": wrapped}]
        }
    }
    provider = AnonymMail(FT("anonymmail", routes))
    address = await provider.generate()
    rows = await provider.list(address)
    message = await provider.get(address, rows[0].id)
    assert message.text == "Hello padded world"


# The inbox lives behind three cookies the homepage sets (am_uid, ci_session,
# csrf_cookie_name); without them /api/get answers 403. The registry runs this
# provider with fresh_session=True, so nothing carries them between requests
# unless the adapter does it, and `cowbird wait` is a different process.
SESSION = {"am_uid": "u1", "ci_session": "s1", "csrf_cookie_name": "c1"}


def session_routes(**overrides):
    from cowbird.testing import Reply

    return {**DEFAULT_ROUTES, "/": Reply(200, "", cookies=SESSION), **overrides}


def posts_to(http, path):
    return [kw for _method, url, kw in http.seen if url.endswith(path)]


async def test_list_replays_the_session_cookies_from_state():
    address = await AnonymMail(fake(session_routes())).generate()
    reader = fake(session_routes())  # a new process: nothing shared but the state
    await AnonymMail(reader).list(address)
    (call,) = posts_to(reader, "/api/get")
    assert call["cookies"]["ci_session"] == "s1"
    assert call["cookies"]["am_uid"] == "u1"


async def test_a_cookie_rotated_mid_generate_wins():
    from cowbird.testing import Reply

    rotated = Reply(200, {"name": "csrf_test_name", "hash": "abc123"},
                    cookies={"csrf_cookie_name": "c2"})
    address = await AnonymMail(fake(session_routes(**{"/api/csrf": rotated}))).generate()
    assert json.loads(address.state)["cookies"]["csrf_cookie_name"] == "c2"


async def test_list_posts_a_csrf_token_fetched_for_that_call():
    address = await AnonymMail(fake(session_routes())).generate()
    reader = fake(session_routes(**{"/api/csrf": {"name": "csrf_test_name", "hash": "fresh9"}}))
    await AnonymMail(reader).list(address)
    (call,) = posts_to(reader, "/api/get")
    assert call["data"]["csrf_test_name"] == "fresh9"


async def test_a_state_without_cookies_is_not_supported():
    from cowbird.errors import NotSupported
    from cowbird.models import Address

    stale = Address("cb1@openstudy.edu.pl", "anonymmail", state='{"csrf": "x"}')
    with pytest.raises(NotSupported):
        await AnonymMail(fake(session_routes())).list(stale)


async def test_a_refused_inbox_read_is_address_expired():
    from cowbird.errors import AddressExpired
    from cowbird.testing import Reply

    address = await AnonymMail(fake(session_routes())).generate()
    reader = fake(session_routes(**{"/api/get": Reply(403, "")}))
    with pytest.raises(AddressExpired):
        await AnonymMail(reader).list(address)


async def test_an_expired_session_reads_as_address_expired_not_drift():
    # Live 2026-11-18: with a stale or missing session, /api/csrf still answers a
    # token, and /api/get then answers 200 with an empty body. Drift would
    # quarantine the provider for everyone over one caller's old --state.
    from cowbird.errors import AddressExpired
    from cowbird.testing import Reply

    address = await AnonymMail(fake(session_routes())).generate()
    reader = fake(session_routes(**{"/api/get": Reply(200, "")}))
    with pytest.raises(AddressExpired):
        await AnonymMail(reader).list(address)
