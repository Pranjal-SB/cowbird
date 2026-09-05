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
