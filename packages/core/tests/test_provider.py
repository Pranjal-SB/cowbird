import pytest
from cowbird.errors import MessageGone, NotSupported
from cowbird.models import Address, MessageRow
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeProvider


async def test_delete_is_not_supported_unless_the_provider_says_so():
    with pytest.raises(NotSupported):
        await FakeProvider().delete(Address("a@fake.test", "fake"), "1")


async def test_default_watch_yields_only_messages_it_has_not_seen():
    rows = lambda *ids: [MessageRow(i, "s@x.test", "hi", None) for i in ids]  # noqa: E731
    provider = FakeProvider(pages=[rows("1"), rows("1"), rows("1", "2")])

    seen = []
    async for msg in provider.watch(Address("a@fake.test", "fake"), poll=0):
        seen.append(msg.id)
        if len(seen) == 2:
            break

    assert seen == ["1", "2"]


async def test_default_watch_ignores_locked_rows():
    provider = FakeProvider(
        pages=[
            [MessageRow("1", "s@x.test", "paywalled", None, locked=True)],
            [MessageRow("2", "s@x.test", "free", None)],
        ]
    )
    async for msg in provider.watch(Address("a@fake.test", "fake"), poll=0):
        assert msg.id == "2"
        break


async def test_default_watch_survives_a_message_that_expires_mid_stream():
    # A message can be listed and be gone by the time get() runs. One expired
    # message must not end the caller's stream.
    class ExpiringGet(FakeProvider):
        async def get(self, address, id):
            if id == "1":
                raise MessageGone("gone: 1")
            return await super().get(address, id)

    rows = [MessageRow("1", "s@x.test", "hi", None), MessageRow("2", "s@x.test", "hi", None)]
    provider = ExpiringGet(pages=[rows])

    stream = provider.watch(Address("a@fake.test", "fake"), poll=0)
    message = await anext(stream)
    assert message.id == "2"
    await stream.aclose()


def test_generate_options_default_to_no_constraints():
    opts = GenerateOptions()
    assert opts.kind is None and opts.local is None and opts.domain is None


@pytest.mark.parametrize(
    "local", ["a/b", "x?y=1", "a#b", "a\r\nHost: evil", "a b", "", "a@b", "x" * 65]
)
def test_a_local_part_that_could_reshape_a_request_is_refused(local):
    # Adapters build paths and queries from the address; a caller's local part
    # must never be able to add a path segment, a query, or a header.
    with pytest.raises(NotSupported):
        GenerateOptions(local=local)


@pytest.mark.parametrize("domain", ["evil.test/x", "a b.test", "a.test?x", ""])
def test_a_domain_that_could_reshape_a_request_is_refused(domain):
    with pytest.raises(NotSupported):
        GenerateOptions(domain=domain)


@pytest.mark.parametrize("local", ["cb12ab", "first.last", "a+tag", "under_score", "dash-ed"])
def test_ordinary_local_parts_are_accepted(local):
    assert GenerateOptions(local=local, domain="mail-edu.eu").local == local
