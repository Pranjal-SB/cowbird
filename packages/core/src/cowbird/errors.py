from __future__ import annotations


class CowbirdError(Exception):
    """Base for everything this library raises."""

    # Provider authors: a new error type that doesn't override this defaults
    # to False, i.e. "surfaced to the caller as-is, never used to reroute to
    # another provider or recorded as a health failure." That is a real
    # decision, not an accident — set True only for errors that mean the
    # provider itself is at fault (down, rate-limited, drifted) rather than
    # errors that are a correct answer to the caller's specific request.
    reroutable: bool = False


class ProviderError(CowbirdError):
    """A provider failed. May or may not be worth retrying elsewhere."""


class ProviderDown(ProviderError):
    """Network failure, 5xx, or timeout."""

    reroutable = True


class CloudflareChallenge(ProviderDown):
    """An active JS challenge was served instead of a response.

    Distinct from ProviderDown because it usually means the egress IP is wrong,
    not that the provider is broken — the health layer records it differently.
    """

    reroutable = True


class RateLimited(ProviderError):
    """Upstream throttled us."""

    reroutable = True


class SchemaDrift(ProviderError):
    """The response parsed, but its shape is not what this adapter knows.

    This is how these services break in practice, so it is loud and it carries
    both halves of the mismatch for the quarantine issue.
    """

    reroutable = True

    def __init__(self, provider: str, expected: str, got: object) -> None:
        self.provider = provider
        self.expected = expected
        self.got = got
        super().__init__(f"{provider}: expected {expected!r}, got {got!r}")


class MessageLocked(ProviderError):
    """The message exists but is behind the provider's paywall."""


class MessageGone(ProviderError):
    """The message existed and the backend no longer has it.

    An answer about one message, not about the provider, so it does not reroute
    and is not recorded as a health failure: a stale row whose storage expired
    upstream must not evict a healthy backend from routing.

    Known blind spot: because it records neither success nor failure, a
    provider whose read path is broken and answers "gone" for every message
    stays green and keeps being routed to; list() latency is all the health
    store sees. That gap is carried by the live canary, not by routing.
    """


class NotSupported(CowbirdError):
    """The provider does not have the requested capability."""


class AddressExpired(CowbirdError):
    """The address outlived its TTL."""


class NoProviderAvailable(CowbirdError):
    """No registered provider satisfied the request, or all of them failed."""

    def __init__(self, tried: list[str], last: Exception | None = None) -> None:
        self.tried = tried
        self.last = last
        detail = f" last error: {last!r}" if last else ""
        super().__init__(f"no provider available (tried: {tried or 'none'}).{detail}")
