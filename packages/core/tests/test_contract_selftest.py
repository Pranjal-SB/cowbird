"""The contract suite has to catch a broken provider, or it proves nothing."""

import pytest
from cowbird.contract import ProviderContract
from cowbird.models import Address
from cowbird.testing import FakeProvider


class TestFakeProviderPasses(ProviderContract):
    @pytest.fixture
    def provider(self):
        return FakeProvider()


async def test_contract_rejects_a_provider_that_errors_on_an_empty_inbox():
    class Broken(FakeProvider):
        async def list(self, address):
            raise RuntimeError("404 not found")

    contract = ProviderContract()
    with pytest.raises(RuntimeError):
        await contract.test_empty_inbox_is_a_list_not_an_error(Broken())


async def test_contract_rejects_an_address_for_the_wrong_provider():
    class Mislabelled(FakeProvider):
        async def generate(self, opts=None):
            return Address(value="a@fake.test", provider="someone-else")

    contract = ProviderContract()
    with pytest.raises(AssertionError):
        await contract.test_generate_returns_an_address_owned_by_this_provider(Mislabelled())


async def test_monotonic_list_accepts_superset_on_second_call():
    """A provider whose second list() returns superset of first should pass."""
    from cowbird.models import MessageRow

    class SupsetListProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        async def list(self, address):
            self.call_count += 1
            if self.call_count == 1:
                return [
                    MessageRow(
                        id="first", sender="a@x.test", subject="1", received_at=None
                    )
                ]
            else:
                return [
                    MessageRow(
                        id="first", sender="a@x.test", subject="1", received_at=None
                    ),
                    MessageRow(
                        id="second", sender="b@x.test", subject="2", received_at=None
                    ),
                ]

    contract = ProviderContract()
    # Should pass, not raise
    await contract.test_listing_is_monotonic(SupsetListProvider())


async def test_monotonic_list_rejects_entirely_different_ids():
    """A provider whose second list() returns different IDs should fail."""
    from cowbird.models import MessageRow

    class NonMonotonicListProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        async def list(self, address):
            self.call_count += 1
            if self.call_count == 1:
                return [
                    MessageRow(
                        id="first", sender="a@x.test", subject="1", received_at=None
                    )
                ]
            else:
                return [
                    MessageRow(
                        id="completely-different",
                        sender="c@x.test",
                        subject="3",
                        received_at=None,
                    )
                ]

    contract = ProviderContract()
    with pytest.raises(AssertionError):
        await contract.test_listing_is_monotonic(NonMonotonicListProvider())


async def test_needs_state_provider_that_issues_no_state_fails():
    """A provider claiming needs_state=True must actually return a state."""
    from dataclasses import replace

    class LiarProvider(FakeProvider):
        caps = replace(FakeProvider.caps, needs_state=True)

        async def generate(self, opts=None):
            return Address(value="a@fake.test", provider=self.name)  # state=None

    contract = ProviderContract()
    with pytest.raises(AssertionError):
        await contract.test_needs_state_providers_actually_issue_state(LiarProvider())


async def test_needs_state_provider_that_issues_state_passes():
    from dataclasses import replace

    class HonestProvider(FakeProvider):
        caps = replace(FakeProvider.caps, needs_state=True)

        async def generate(self, opts=None):
            return Address(value="a@fake.test", provider=self.name, state="creds")

    contract = ProviderContract()
    # Should pass, not raise
    await contract.test_needs_state_providers_actually_issue_state(HonestProvider())


def test_domain_count_less_than_declared_domains_fails():
    """domain_count < len(domains) should fail capability check."""
    from cowbird.models import Capabilities, Kind

    class BadDomainCountProvider(FakeProvider):
        caps = Capabilities(
            kind=frozenset({Kind.OWN_DOMAIN}),
            sites=("bad.test",),
            domains=("a.test", "b.test"),  # 2 declared
            domain_count=1,  # But domain_count is less!
            address_ttl=None,
            message_ttl=FakeProvider.caps.message_ttl,
            push=False,
            delete=False,
            custom_local=False,
            self_hosted=False,
            needs_residential_ip=False,
        )

    contract = ProviderContract()
    with pytest.raises(AssertionError):
        contract.test_declares_a_name_and_capabilities(BadDomainCountProvider())


def test_empty_domains_with_high_domain_count_passes():
    """Empty domains (runtime discovery) with any domain_count should pass."""
    from cowbird.models import Capabilities, Kind

    class DynamicDomainProvider(FakeProvider):
        caps = Capabilities(
            kind=frozenset({Kind.OWN_DOMAIN}),
            sites=("dynamic.test",),
            domains=(),  # Empty = runtime discovery
            domain_count=50,  # Can be any number
            address_ttl=None,
            message_ttl=FakeProvider.caps.message_ttl,
            push=False,
            delete=False,
            custom_local=False,
            self_hosted=False,
            needs_residential_ip=False,
        )

    contract = ProviderContract()
    # Should pass, not raise
    contract.test_declares_a_name_and_capabilities(DynamicDomainProvider())


async def test_contract_rejects_a_provider_that_reissues_one_address():
    # The session-cookie bug: a shared jar hands every generate() the same
    # inbox. The contract has to catch it for every provider, present and future.
    class Sticky(FakeProvider):
        async def generate(self, opts=None):
            return Address(value="same@fake.test", provider=self.name)

    with pytest.raises(AssertionError, match="fresh_session"):
        await ProviderContract().test_two_generates_yield_distinct_addresses(Sticky())
