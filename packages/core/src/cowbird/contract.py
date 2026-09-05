"""The contract every provider must satisfy.

Shipped in `src/`, not `tests/`, because provider packages import it. A
provider subclasses `ProviderContract`, supplies a `provider` fixture, and
inherits the whole suite. The same class runs mocked in CI and live against the
real service on a schedule — the assertions do not change, only the transport
behind the fixture does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from cowbird.models import Address, Message, MessageRow
from cowbird.provider import GenerateOptions, Provider


def _unwrap_health_tracked(provider: Provider) -> Provider:
    """Unwrap HealthTracked wrapper if present.

    HealthTracked overrides both watch() and delete() to add health tracking.
    The contract checks if a provider overrides these methods to detect push and
    delete support. Without unwrapping, every wrapped provider would look like it
    claims push support and delete support, which is a lie. This helper uses
    getattr() instead of importing HealthTracked to avoid circular imports.
    """
    return getattr(provider, "_inner", provider)


class ProviderContract:
    def test_declares_a_name_and_capabilities(self, provider: Provider) -> None:
        assert isinstance(provider.name, str) and provider.name
        assert provider.caps.kind, "a provider must serve at least one kind"
        assert provider.caps.sites, "a provider must declare its front doors"
        assert provider.caps.domain_count >= 0

    def test_capability_flags_agree_with_the_implementation(
        self, provider: Provider
    ) -> None:
        # A provider claiming push must actually override the polling default.
        # Unwrap HealthTracked first since it overrides watch() for tracking.
        unwrapped = _unwrap_health_tracked(provider)
        overrides_watch = type(unwrapped).watch is not Provider.watch
        assert provider.caps.push == overrides_watch
        overrides_delete = type(unwrapped).delete is not Provider.delete
        assert provider.caps.delete == overrides_delete

    async def test_generate_returns_an_address_owned_by_this_provider(
        self, provider: Provider
    ) -> None:
        address = await provider.generate(GenerateOptions())
        assert isinstance(address, Address)
        assert "@" in address.value
        assert address.provider == provider.name

    async def test_generated_domain_is_one_the_provider_claims(
        self, provider: Provider
    ) -> None:
        address = await provider.generate(GenerateOptions())
        if provider.caps.domains:
            assert address.value.split("@")[1] in provider.caps.domains

    async def test_empty_inbox_is_a_list_not_an_error(self, provider: Provider) -> None:
        address = await provider.generate(GenerateOptions())
        rows = await provider.list(address)
        assert isinstance(rows, list)
        assert all(isinstance(r, MessageRow) for r in rows)

    async def test_listing_twice_is_stable(self, provider: Provider) -> None:
        address = await provider.generate(GenerateOptions())
        assert await provider.list(address) == await provider.list(address)

    async def test_get_returns_a_message_with_text(self, provider: Provider) -> None:
        address = await provider.generate(GenerateOptions())
        rows = await provider.list(address)
        if not rows:
            pytest.skip("no message available in this fixture")
        message = await provider.get(address, rows[0].id)
        assert isinstance(message, Message)
        assert message.id == rows[0].id
        assert isinstance(message.text, str)

    async def test_watch_is_an_async_iterator(self, provider: Provider) -> None:
        stream = provider.watch(Address("probe@example.test", provider.name), poll=0)
        assert isinstance(stream, AsyncIterator)
        await stream.aclose()
