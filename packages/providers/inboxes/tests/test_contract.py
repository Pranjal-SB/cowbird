import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird_inboxes import DOMAINS, Inboxes

FIXTURES = Path(__file__).parent / "fixtures"
ADDRESS = Address("cb0123456789@dropjar.com", "inboxes")


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeTransport:
    """Replays recorded upstream responses.

    Longest-key-first matching: `/message/` and `/inbox/` are distinct here, but
    the rule costs nothing and every other provider's fake needs it.
    """

    provider = "inboxes"

    def __init__(self, routes, status=200):
        self.routes = routes
        self.status = status
        self.seen = []

    def _match(self, url):
        for key in sorted(self.routes, key=len, reverse=True):
            if key in url:
                return self.routes[key]
        raise AssertionError(f"unexpected request: {url}")

    async def json(self, method, url, **kw):
        self.seen.append((method, url, kw.get("json")))
        payload = self._match(url)
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def text(self, method, url, **kw):
        self.seen.append((method, url, kw.get("json")))
        payload = self._match(url)
        return payload if isinstance(payload, str) else json.dumps(payload)

    async def send(self, method, url, **kw):
        self.seen.append((method, url, kw.get("json")))
        payload = self._match(url)
        status = self.status

        class R:
            status_code = status
            text = payload if isinstance(payload, str) else json.dumps(payload)

        return R()

    async def aclose(self):
        pass


MESSAGE = load("message_one.json")
DEFAULT_ROUTES = {
    "/domain": load("domains.json"),
    "/inbox/": load("inbox_full.json"),
    "/message/": MESSAGE,
}


class TestInboxesContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        # The contract reads rows[0].id then calls get(), so the message fixture
        # must be the first row of the list fixture. Keep them in step.
        rows = load("inbox_full.json")
        rows["msgs"][0]["uid"] = MESSAGE["uid"]
        return Inboxes(FakeTransport({**DEFAULT_ROUTES, "/inbox/": rows}))


async def test_generate_makes_no_http_call_at_all():
    # This is the provider's distinguishing behaviour and nothing else asserts
    # it. A catch-all backend needs no registration, so a round-trip here would
    # be latency that can only fail.
    http = FakeTransport(dict(DEFAULT_ROUTES))
    address = await Inboxes(http).generate(GenerateOptions())
    assert http.seen == []
    assert address.provider == "inboxes"
    assert address.value.split("@")[1] in DOMAINS


async def test_generate_honours_a_caller_supplied_local_part():
    http = FakeTransport(dict(DEFAULT_ROUTES))
    address = await Inboxes(http).generate(GenerateOptions(local="chosen-name"))
    assert address.value.startswith("chosen-name@")
    assert http.seen == []


async def test_generate_honours_a_caller_supplied_domain():
    http = FakeTransport(dict(DEFAULT_ROUTES))
    address = await Inboxes(http).generate(GenerateOptions(domain="guysmail.com"))
    assert address.value.endswith("@guysmail.com")


async def test_generate_refuses_a_domain_this_provider_does_not_serve():
    # NotSupported is an answer to the caller, not a provider fault: it must
    # never mark inboxes down or trigger a reroute.
    http = FakeTransport(dict(DEFAULT_ROUTES))
    with pytest.raises(NotSupported) as caught:
        await Inboxes(http).generate(GenerateOptions(domain="not-ours.test"))
    assert caught.value.reroutable is False


async def test_generated_addresses_are_not_all_the_same():
    http = FakeTransport(dict(DEFAULT_ROUTES))
    provider = Inboxes(http)
    values = {(await provider.generate(GenerateOptions())).value for _ in range(20)}
    assert len(values) > 1, "generate() is issuing a constant address"


async def test_an_empty_inbox_is_an_empty_list():
    # Recorded live: HTTP 200 {"msgs": []}, not a 404 and not an error envelope.
    http = FakeTransport({**DEFAULT_ROUTES, "/inbox/": load("inbox_empty.json")})
    assert await Inboxes(http).list(ADDRESS) == []


async def test_message_list_in_an_unexpected_shape_raises_schema_drift():
    http = FakeTransport({**DEFAULT_ROUTES, "/inbox/": {"messages": []}})
    with pytest.raises(SchemaDrift, match="msgs"):
        await Inboxes(http).list(ADDRESS)


async def test_row_missing_uid_raises_schema_drift_naming_uid():
    broken = {"msgs": [{"f": "a@b.test", "s": "hi"}]}
    http = FakeTransport({**DEFAULT_ROUTES, "/inbox/": broken})
    with pytest.raises(SchemaDrift, match="uid"):
        await Inboxes(http).list(ADDRESS)


async def test_terse_row_fields_are_mapped_explicitly():
    # uid/f/s/r are undocumented and do not match the v3 spec's nicer names.
    rows = await Inboxes(FakeTransport(dict(DEFAULT_ROUTES))).list(ADDRESS)
    first = load("inbox_full.json")["msgs"][0]
    assert rows[0].id == first["uid"]
    assert rows[0].sender == first["f"]
    assert rows[0].subject == first["s"]
    assert rows[0].received_at == datetime.fromtimestamp(int(first["r"]), UTC)


async def test_get_prefers_the_servers_own_plain_text_over_flattened_html():
    doc = {"uid": "x", "text": "Code is 4242", "html": "<p>ignore this</p>", "s": "", "f": ""}
    http = FakeTransport({**DEFAULT_ROUTES, "/message/": doc})
    message = await Inboxes(http).get(ADDRESS, "x")
    assert message.text == "Code is 4242"


async def test_a_null_text_field_falls_back_rather_than_yielding_none():
    # The recorded message carries `text` as an explicit null: the key is
    # present, the value is not. `"text" in payload` is therefore the wrong
    # check, and a caller who got None here would see otp() fail on a message
    # whose HTML holds the code perfectly well.
    assert MESSAGE["text"] is None
    message = await Inboxes(FakeTransport(dict(DEFAULT_ROUTES))).get(ADDRESS, MESSAGE["uid"])
    assert message.id == MESSAGE["uid"]
    assert message.html == MESSAGE["html"]
    assert message.text and "Home Depot" in message.text


async def test_get_falls_back_to_flattening_html_when_text_is_absent():
    doc = {"uid": "x", "html": "<p>Code is <b>4242</b></p>", "s": "s", "f": "f"}
    http = FakeTransport({**DEFAULT_ROUTES, "/message/": doc})
    message = await Inboxes(http).get(ADDRESS, "x")
    assert "4242" in message.text
    assert "<p>" not in message.text


async def test_get_without_a_uid_raises_schema_drift():
    http = FakeTransport({**DEFAULT_ROUTES, "/message/": {"html": "<p>hi</p>"}})
    with pytest.raises(SchemaDrift, match="uid"):
        await Inboxes(http).get(ADDRESS, "x")


async def test_delete_sends_the_uid_in_the_body_not_the_url():
    # The endpoint is bulk: DELETE /message/ with {"ids": [...]}. Putting the id
    # in the path silently deletes nothing.
    http = FakeTransport({**DEFAULT_ROUTES, "/message/": {"changed": True}})
    await Inboxes(http).delete(ADDRESS, "some-uid")
    method, url, body = http.seen[-1]
    assert method == "DELETE"
    assert url.endswith("/message/")
    assert body == {"ids": ["some-uid"]}
