from __future__ import annotations

from cowbird.errors import (
    AddressExpired,
    CowbirdError,
    MessageGone,
    MessageLocked,
    NoProviderAvailable,
    NotSupported,
    ProviderError,
    RateLimited,
    SolverUnavailable,
)

# Ordered most specific first: the first isinstance match wins, so a subclass
# never inherits its parent's status by accident. CloudflareChallenge is a
# ProviderDown and SchemaDrift is a ProviderError; both land on 502 through the
# ProviderError row at the end.
_MAPPING: tuple[tuple[type[CowbirdError], int, str], ...] = (
    (RateLimited, 429, "upstream rate limited"),
    (MessageLocked, 423, "the message is behind the provider's paywall"),
    (MessageGone, 410, "that message is no longer available"),
    (NoProviderAvailable, 503, "no provider available for that request"),
    (NotSupported, 422, "no provider supports that combination of options"),
    (AddressExpired, 410, "that address has expired"),
    (SolverUnavailable, 503, "cloudflare solver unavailable"),
    (ProviderError, 502, "upstream provider error"),
)


def status_for(exc: CowbirdError) -> tuple[int, str]:
    """The HTTP status and the message a client is allowed to see.

    The message is fixed per class rather than `str(exc)`. Library errors carry
    diagnostic detail meant for a maintainer reading a quarantine issue:
    SchemaDrift in particular stringifies to the payload it got from the
    backend, and putting that in a response body hands a caller a slice of a
    third party's response. The full exception goes to the log instead.
    """
    for cls, status, message in _MAPPING:
        if isinstance(exc, cls):
            return status, message
    return 500, "internal error"
