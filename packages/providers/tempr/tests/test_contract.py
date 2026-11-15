"""Contract + unit tests for the tempr.email provider.

The fixtures in fixtures/ are raw captures: sse_idle.txt is the literal first
frame the live SSE watch sends on connect (2026-11-15), sse_message.txt the
literal frame a delivered mail produced. Both were recorded through the same
curl_cffi h2 client the adapter runs (see docs/recon/tempr-followup.md).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, ProviderDown, SchemaDrift
from cowbird.models import Address, InboxPrivacy, Kind
from cowbird.testing import FakeTransport, Reply
from cowbird.transport import Transport
from cowbird_tempr import Tempr, parse_sse

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def fake(routes: dict) -> FakeTransport:
    return FakeTransport("tempr", routes, 200)


SID = "40a9f2f51ddeda66b966944d139a4efb"
CSRF = "71ed1df0af5b0a3c4d5e6f7a8b9c0d1e"

HOME_HTML = (
    '<select id="login-domains"><option value="tempr.email">'
    '<option value="trashmailr.com"></select>'
    f'<input name="login-csrf" value="{CSRF}">'
)

DEFAULT_ROUTES = {
    "GET https://tempr.email/en/": Reply(
        200,
        HOME_HTML,
        cookies={"sid": SID},
    ),
    "POST https://tempr.email/en/inbox/list.htm": '<div id="inbox-messages-list"></div>',
}


# --- parse_sse: pure function over captured frames ---------------------------


def test_parse_sse_over_the_captured_idle_frame():
    patches = parse_sse(load("sse_idle.txt"))
    assert len(patches) == 1
    assert patches[0].event == "datastar-patch-elements"
    assert 'id="inbox-messages-list"' in patches[0].elements
    assert "data-init=" in patches[0].elements


def test_parse_sse_over_the_captured_message_frame():
    patches = parse_sse(load("sse_message.txt"))
    assert len(patches) == 1
    assert 'data-message="' in patches[0].elements


def test_parse_sse_empty_or_idle_text_yields_no_patches():
    assert parse_sse("") == []
    assert parse_sse("\n\n") == []


def test_parse_sse_splits_multiple_frames():
    two = load("sse_idle.txt") + "\n" + load("sse_message.txt")
    assert len(parse_sse(two)) == 2


def test_parse_sse_tolerates_datastar_signal_frames():
    # Datastar also ships datastar-patch-signals frames; they must parse to a
    # named patch rather than blowing up the parser.
    patches = parse_sse('event:datastar-patch-signals\ndata:signals {"_empty": 1}\n\n')
    assert len(patches) == 1
    assert patches[0].event == "datastar-patch-signals"


# --- watch: bounded and cancellation-safe ------------------------------------


class SilentStream:
    """A fake SSE stream whose chunks never arrive."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()  # never


async def test_watch_on_a_silent_stream_is_bounded_not_hung():
    provider = Tempr(fake(dict(DEFAULT_ROUTES)))
    provider._sse = lambda *a, **kw: SilentStream()  # type: ignore[method-assign]
    address = Address("probe@tempr.email", "tempr")
    stream = provider.watch(address, poll=0.05)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    await stream.aclose()


async def test_watch_surfaces_a_persistent_outage_after_bounded_reconnects():
    # A mid-stream blip reconnects; a persistent outage must not spin forever:
    # after MAX_RECONNECTS consecutive failed connects the ProviderDown
    # propagates to the caller.
    provider = Tempr(fake(dict(DEFAULT_ROUTES)))

    async def down(*a, **kw):
        raise ProviderDown("tempr: mta unreachable")
        yield  # pragma: no cover

    provider._sse = down  # type: ignore[method-assign]
    address = Address("probe@tempr.email", "tempr")
    stream = provider.watch(address, poll=0.01)
    with pytest.raises(ProviderDown):
        await asyncio.wait_for(stream.__anext__(), timeout=5.0)
    await stream.aclose()


async def test_list_propagates_an_outage_instead_of_returning_empty():
    # An empty inbox is [] after the idle bound; a dead endpoint is a
    # ProviderDown. The two must not be conflated.
    provider = Tempr(fake(dict(DEFAULT_ROUTES)))

    async def down(*a, **kw):
        raise ProviderDown("tempr: mta unreachable")
        yield  # pragma: no cover

    provider._sse = down  # type: ignore[method-assign]
    with pytest.raises(ProviderDown):
        await provider.list(Address("probe@tempr.email", "tempr"))


async def test_the_watch_session_matches_the_transport_egress(monkeypatch):
    # The watch bypasses Transport (no streaming API) but must not bypass its
    # egress: same impersonation, same proxy.
    captured: dict = {}

    class RecordingSession:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("cowbird_tempr.AsyncSession", RecordingSession)
    http = Transport("tempr", proxy="http://10.0.0.1:8080", impersonate="chrome131")
    provider = Tempr(http)
    provider._session()
    assert captured["impersonate"] == "chrome131"
    assert captured["proxies"] == {"http": "http://10.0.0.1:8080", "https": "http://10.0.0.1:8080"}

    captured.clear()
    http = Transport("tempr")
    Tempr(http)._session()
    assert captured["proxies"] is None


async def test_watch_is_cancellation_safe():
    provider = Tempr(fake(dict(DEFAULT_ROUTES)))
    provider._sse = lambda *a, **kw: SilentStream()  # type: ignore[method-assign]
    address = Address("probe@tempr.email", "tempr")
    stream = provider.watch(address, poll=0.05)
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()


async def test_watch_yields_messages_parsed_from_captured_frames():
    routes = {
        **DEFAULT_ROUTES,
        "GET https://mta.trashmailr.com:81/app/message/"
        "mail@tempr.email/01M6NWK36XH4J39X87V8955Q6Z": load("message_body.txt"),
    }
    provider = Tempr(fake(routes))
    provider._sse = lambda *a, **kw: FakeStream([load("sse_message.txt")])  # type: ignore[method-assign]
    address = Address("mail@tempr.email", "tempr")
    stream = provider.watch(address, poll=0.05)
    # watch() reconnects forever on stream end (that is the site's own
    # behaviour), so bound the take explicitly instead of iterating to exhaustion.
    message = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
    await stream.aclose()
    # The row frame supplies the id; the body patch supplies the fields.
    assert message.id == "01M6NWK36XH4J39X87V8955Q6Z"
    assert message.sender == "sender.one@example.test"
    assert message.subject == "Your Payment Information"


class FakeStream:
    """Replays captured chunks, then ends the stream (as an idle kill would)."""

    def __init__(self, chunks: list[str]):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


# --- contract suite ----------------------------------------------------------


class TestTemprContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        http = fake(
            {
                **DEFAULT_ROUTES,
                "POST https://tempr.email/en/inbox/list.htm": (
                    '<div id="inbox-messages-list"></div>'
                ),
                CONTRACT_BODY_ROUTE: load("message_body.txt"),
            }
        )
        provider = Tempr(http)
        # The SSE watch bypasses Transport (raw curl_cffi h2 stream — Transport
        # has no streaming API), so tests inject the captured frames here. The
        # row frame gives list() a message so the contract's body-read path is
        # exercised against the captured body-patch route below.
        provider._sse = lambda *a, **kw: FakeStream([load("sse_message.txt")])  # type: ignore[method-assign]
        return provider


CONTRACT_BODY_ROUTE = "01M6NWK36XH4J39X87V8955Q6Z"


async def test_declares_public_inbox_privacy():
    assert Tempr.caps.inbox_privacy is InboxPrivacy.PUBLIC
    assert Kind.OWN_DOMAIN in Tempr.caps.kind


async def test_generate_packs_sid_and_csrf_into_state():
    provider = Tempr(fake(dict(DEFAULT_ROUTES)))
    address = await provider.generate()
    assert address.value.endswith("@tempr.email")
    state = json.loads(address.state or "{}")
    assert state["sid"] == SID
    assert state["csrf"] == CSRF


async def test_generate_sends_the_honeypot_empty_and_the_csrf():
    http = fake(dict(DEFAULT_ROUTES))
    await Tempr(http).generate()
    ((_, url, kw),) = [(m, u, k) for m, u, k in http.seen if u.endswith("inbox/list.htm")]
    assert kw["data"]["login-hp"] == ""
    assert kw["data"]["login-csrf"] == CSRF
    assert kw["headers"]["X-Requested-With"] == "XMLHttpRequest"


async def test_a_requested_domain_the_provider_does_not_scrape_is_refused():
    provider = Tempr(fake(dict(DEFAULT_ROUTES)))
    from cowbird.errors import NotSupported
    from cowbird.provider import GenerateOptions

    with pytest.raises(NotSupported):
        await provider.generate(GenerateOptions(domain="not-a-domain.test"))


async def test_missing_csrf_is_schema_drift():
    http = fake({"GET https://tempr.email/en/": "<html>no token</html>"})
    with pytest.raises(SchemaDrift):
        await Tempr(http).generate()


# --- delete: the row's trash button, captured live --------------------------


def _address() -> Address:
    return Address("cowbirdt7x1@tempr.email", "tempr")


async def test_delete_sends_delete_to_the_mta_host_without_state():
    # The row markup's data-on:click="@delete('https://mta.../app/message/<addr>/<id>')"
    # — no sid, no csrf: the address in the path authorizes (PUBLIC inbox).
    http = fake({"DELETE https://mta.trashmailr.com:81/app/message/cowbirdt7x1@tempr.email/m1": ""})
    await Tempr(http).delete(_address(), "m1")
    ((method, url, _),) = http.seen
    assert (method, url) == (
        "DELETE",
        "https://mta.trashmailr.com:81/app/message/cowbirdt7x1@tempr.email/m1",
    )


async def test_delete_on_404_is_treated_as_already_gone():
    http = fake(
        {
            "DELETE https://mta.trashmailr.com:81/app/message/cowbirdt7x1@tempr.email/m1": Reply(
                404, ""
            )
        }
    )
    assert await Tempr(http).delete(_address(), "m1") is None


async def test_delete_on_4xx_raises_provider_down():
    http = fake(
        {
            "DELETE https://mta.trashmailr.com:81/app/message/cowbirdt7x1@tempr.email/m1": Reply(
                403, ""
            )
        }
    )
    with pytest.raises(ProviderDown):
        await Tempr(http).delete(_address(), "m1")


# --- get: the body patch, captured live --------------------------------------

BODY_ROUTE = "GET https://mta.trashmailr.com:81/app/message/cowbirdt7x1@tempr.email/m1"


async def test_get_parses_sender_subject_and_body_from_the_captured_patch():
    http = fake({BODY_ROUTE: load("message_body.txt")})
    message = await Tempr(http).get(_address(), "m1")
    assert message.sender == "sender.one@example.test"
    assert message.subject == "Your Payment Information"
    assert "Your order has been confirmed" in message.text
    assert "<table" in message.html


async def test_get_on_404_raises_message_gone():
    http = fake({BODY_ROUTE: Reply(404, "")})
    with pytest.raises(MessageGone):
        await Tempr(http).get(_address(), "m1")


async def test_get_on_5xx_raises_provider_down():
    http = fake({BODY_ROUTE: Reply(500, "boom")})
    with pytest.raises(ProviderDown):
        await Tempr(http).get(_address(), "m1")


async def test_a_non_patch_body_is_schema_drift():
    http = fake({BODY_ROUTE: "<html>not sse</html>"})
    with pytest.raises(SchemaDrift):
        await Tempr(http).get(_address(), "m1")
