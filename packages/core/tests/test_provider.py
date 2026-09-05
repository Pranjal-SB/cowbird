import pytest
from cowbird.errors import NotSupported
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


def test_generate_options_default_to_no_constraints():
    opts = GenerateOptions()
    assert opts.kind is None and opts.local is None and opts.domain is None
