from dataclasses import replace

import pytest
from cowbird.health import HealthStore
from cowbird.inbox import Inbox, aclose_default_pool, default_pool, open_inbox, open_inboxes
from cowbird.models import Address, Message, MessageRow
from cowbird.parsing import extract_links
from cowbird.pool import Pool
from cowbird.registry import Registry
from cowbird.testing import CAPS, FakeProvider

HTML = '<p>Code 448213</p><a href="https://verify.test/go">here</a>'


class Recording(FakeProvider):
    name = "rec"
    caps = CAPS

    async def get(self, address, id):
        return Message(
            id=id,
            sender="s@x.test",
            subject="Verify",
            received_at=None,
            html=HTML,
            # links must be populated here: Message.links defaults to () and
            # Inbox.link() reads the field rather than re-parsing the html.
            # A provider that forgets this makes link() silently time out.
            links=extract_links(HTML),
            text="Code 448213",
        )


def pool_with(cls):
    reg = Registry(transport_factory=lambda name: None, discover=False)
    reg.register(cls)
    return Pool(reg, HealthStore())


async def test_otp_waits_for_mail_then_returns_the_code():
    cls = type("P", (Recording,), {"name": "p"})
    provider = cls(pages=[[], [MessageRow("1", "s@x.test", "Verify", None)]])
    box = Inbox(provider, Address("a@fake.test", "p"))
    assert await box.otp(timeout=5, poll=0) == "448213"


async def test_link_returns_the_first_safe_href():
    cls = type("P", (Recording,), {"name": "p"})
    provider = cls(pages=[[MessageRow("1", "s@x.test", "Verify", None)]])
    box = Inbox(provider, Address("a@fake.test", "p"))
    assert await box.link(timeout=5, poll=0) == "https://verify.test/go"


async def test_otp_raises_timeout_error_when_no_mail_arrives():
    cls = type("P", (Recording,), {"name": "p"})
    provider = cls(pages=[[], [], []])
    box = Inbox(provider, Address("a@fake.test", "p"))
    with pytest.raises(TimeoutError):
        await box.otp(timeout=0.05, poll=0.01)


async def test_open_inbox_yields_a_bound_inbox():
    async with open_inbox(pool=pool_with(Recording)) as box:
        assert box.address.value == "a@fake.test"
        assert box.provider.name == "rec"


async def test_open_inboxes_returns_the_requested_count():
    async with open_inboxes(3, pool=pool_with(Recording)) as boxes:
        assert len(boxes) == 3


async def test_leaving_the_context_deletes_when_the_provider_supports_it():
    seen = []

    class Deleting(Recording):
        name = "del"
        # Must be replace(CAPS, delete=True), not CAPS: aclose() checks the
        # capability before cleaning up, so a provider that overrides delete()
        # while declaring delete=False would silently never be called.
        caps = replace(CAPS, delete=True)

        async def delete(self, address, id):
            seen.append(id)

    async with open_inbox(pool=pool_with(Deleting)) as box:
        box._issued.append("1")
    assert seen == ["1"]


async def test_otp_raises_with_stream_ended_message_when_watch_yields_nothing():
    class FiniteWatch(Recording):
        name = "finite"

        async def watch(self, address, poll=None):
            # Finite generator that yields nothing
            return
            yield  # noqa: F701

    cls = type("P", (FiniteWatch,), {"name": "p"})
    provider = cls(pages=[])
    box = Inbox(provider, Address("a@fake.test", "p"))
    with pytest.raises(TimeoutError, match="mail stream.*ended before a match"):
        await box.otp(timeout=5, poll=0)


async def test_open_inboxes_cleans_up_on_acquisition_failure():
    aclose_calls: list[Address] = []
    original_aclose = Inbox.aclose

    async def track_aclose(self):
        aclose_calls.append(self.address)
        await original_aclose(self)

    class Failing(Recording):
        name = "fail"

        async def generate(self, *args, **kwargs):
            raise ValueError("acquisition failed")

    # Pool with two providers: one that succeeds, one that fails.
    reg = Registry(transport_factory=lambda name: None, discover=False)
    reg.register(Recording)
    reg.register(Failing)
    pool = Pool(reg, HealthStore())

    # Patch Inbox.aclose to track calls.
    Inbox.aclose = track_aclose
    try:
        # Try to acquire 2 inboxes: first from Recording, second from Failing.
        # The first should be acquired and cleaned up.
        with pytest.raises(ValueError, match="acquisition failed"):
            async with open_inboxes(2, pool=pool):
                pass

        # Verify the exception propagated and aclose was called on the
        # successfully-acquired inbox (first one from Recording provider).
        assert len(aclose_calls) == 1
        assert aclose_calls[0].value == "a@fake.test"
    finally:
        Inbox.aclose = original_aclose


async def test_aclose_default_pool_resets_the_global():
    pool1 = default_pool()
    await aclose_default_pool()
    pool2 = default_pool()
    assert pool1 is not pool2
