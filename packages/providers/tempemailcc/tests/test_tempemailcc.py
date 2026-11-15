"""tempemail.cc adapter — parsing and behavior pinned against recorded fixtures."""

import json
from pathlib import Path

import pytest
from cowbird.errors import SchemaDrift
from cowbird.models import Address, InboxPrivacy, Kind
from cowbird.testing import FakeTransport
from cowbird_tempemailcc import TempEmailCc

FIXTURES = Path(__file__).parent / "fixtures"
MID = "247827277901992001"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    return {
        "POST https://www.tempemail.cc/api/accounts": json.loads(load("create.json")),
        "GET https://www.tempemail.cc/api/messages": json.loads(load("messages_one.json")),
        "GET https://www.tempemail.cc/api/messages/" + MID: json.loads(load("message_detail.json")),
        **overrides,
    }


def make(**overrides):
    return TempEmailCc(FakeTransport("tempemailcc", routes(**overrides)))


def state():
    return json.dumps({"token": "t.fake.t", "password": "pw7"})


async def test_generate_returns_address_with_jwt_state():
    address = await make().generate()
    assert address.value.endswith("@icmans.com")
    assert address.provider == "tempemailcc"
    assert json.loads(address.state)["token"]


async def test_generate_posts_email_and_password():
    transport = FakeTransport("tempemailcc", routes())
    await TempEmailCc(transport).generate()
    posts = [s for s in transport.seen if s[0] == "POST"]
    assert posts and posts[0][1] == "https://www.tempemail.cc/api/accounts"
    assert posts[0][2]["json"]["email"].endswith("@icmans.com")


async def test_caps_match_the_survey():
    caps = TempEmailCc.caps
    assert caps.needs_state
    assert caps.domains == ("icmans.com",)
    assert Kind.OWN_DOMAIN in caps.kind
    assert caps.inbox_privacy == InboxPrivacy.PRIVATE


async def test_list_walks_cursor_pagination():
    page1 = json.loads(load("messages_one.json"))
    page2 = json.loads(load("messages_one.json"))
    page2["data"]["items"] = []
    page1["data"]["pagination"]["nextCursor"] = "cursor-9"
    transport = FakeTransport(
        "tempemailcc",
        {
            **routes(),
            "GET https://www.tempemail.cc/api/messages": page1,
            "GET https://www.tempemail.cc/api/messages?limit=50&cursor=cursor-9": page2,
        },
    )
    provider = TempEmailCc(transport)
    address = Address("cbprobe@icmans.com", "tempemailcc",
                      state=state())
    rows = await provider.list(address)
    assert [r.id for r in rows] == [row["id"] for row in page1["data"]["items"]]
    assert any("cursor=cursor-9" in seen[1] for seen in transport.seen)


async def test_list_empty_inbox_is_empty_list():
    empty = json.loads(load("messages_empty.json"))
    provider = make(**{"GET https://www.tempemail.cc/api/messages": empty})
    address = Address("cbprobe@icmans.com", "tempemailcc", state=state())
    assert await provider.list(address) == []


async def test_get_returns_message_with_text_and_links():
    provider = make()
    address = Address("cbprobe@icmans.com", "tempemailcc", state=state())
    message = await provider.get(address, MID)
    assert message.id == MID
    assert message.sender
    assert message.text
    assert isinstance(message.links, tuple)


async def test_missing_token_in_create_is_schema_drift():
    bad = json.loads(load("create.json"))
    del bad["data"]["token"]
    provider = make(**{"POST https://www.tempemail.cc/api/accounts": bad})
    with pytest.raises(SchemaDrift):
        await provider.generate()


async def test_non_200_code_in_envelope_is_schema_drift():
    bad = json.loads(load("messages_one.json"))
    bad["code"] = 500
    provider = make(**{"GET https://www.tempemail.cc/api/messages": bad})
    address = Address("cbprobe@icmans.com", "tempemailcc", state=state())
    with pytest.raises(SchemaDrift):
        await provider.list(address)


class GzipStub:
    """A transport whose /body answer is raw gzip bytes, which the real
    backend serves as application/gzip regardless of Accept-Encoding."""

    def __init__(self, body: bytes):
        self.body = body
        self.provider = "tempemailcc"

    async def json(self, method, url, **kw):
        return {
            "code": 200,
            "message": "success",
            "data": {"id": MID, "from": {"address": "noreply@example.test"},
                     "subject": "hi", "createdAt": 1794748058},
        }

    async def send(self, method, url, **kw):
        class Resp:
            status_code = 200
            content = self.body
            text = ""
        return Resp()

    async def aclose(self):
        pass


async def test_get_decompresses_gzip_body():
    import gzip as gzip_mod

    provider = TempEmailCc(GzipStub(gzip_mod.compress(b"<html><a href='https://x.test'>go</a></html>")))
    message = await provider.get(
        Address("cbprobe@icmans.com", "tempemailcc", state=state()), MID
    )
    assert "go" in message.text
    assert message.links == ("https://x.test",)


async def test_corrupt_gzip_body_is_schema_drift():
    provider = TempEmailCc(GzipStub(b"\x1f\x8btruncated"))
    with pytest.raises(SchemaDrift):
        await provider.get(
            Address("cbprobe@icmans.com", "tempemailcc", state=state()), MID
        )


async def test_envelope_429_is_rate_limited():
    from cowbird.errors import RateLimited

    throttled = json.loads(load("create.json"))
    throttled["code"] = 429
    provider = make(**{"POST https://www.tempemail.cc/api/accounts": throttled})
    with pytest.raises(RateLimited):
        await provider.generate()


async def test_corrupt_deflate_stream_is_schema_drift():
    import gzip as gzip_mod

    # Valid gzip header, corrupt deflate data mid-stream: raises zlib.error,
    # not OSError — the guard must catch both.
    good = gzip_mod.compress(b"hello" * 10)
    provider = TempEmailCc(GzipStub(good[:len(good) // 2] + good[len(good) // 2 + 1:]))
    with pytest.raises(SchemaDrift):
        await provider.get(
            Address("cbprobe@icmans.com", "tempemailcc", state=state()), MID
        )
