from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class Kind(StrEnum):
    GMAIL_ALIAS = "gmail-alias"
    OUTLOOK_ALIAS = "outlook-alias"
    OWN_DOMAIN = "own-domain"
    EDU = "edu"


@dataclass(frozen=True, slots=True)
class Capabilities:
    kind: frozenset[Kind]
    sites: tuple[str, ...]
    domains: tuple[str, ...]
    domain_count: int
    address_ttl: timedelta | None
    message_ttl: timedelta | None
    push: bool
    delete: bool
    custom_local: bool
    self_hosted: bool
    needs_residential_ip: bool
    # True when generate() issues credentials or a session that later calls
    # (list/get/watch/delete) require. A caller resuming an address out of
    # process — a separate `cowbird wait` after `cowbird new` — must carry
    # `Address.state` with it or those calls cannot authenticate.
    needs_state: bool = False
    # mail.tm publishes 8 QPS per IP. `inboxes(20)` would fire twenty concurrent
    # generates, collect twenty 429s, and empty the pool of the one provider
    # that was working. A backend's tolerance belongs to the backend.
    max_concurrency: int = 4
    # A 5-second default is nonsense for a backend whose list() takes 6.5s and
    # whose first body read takes 39s. Polling cadence is per-provider too.
    poll_interval: float = 5.0
    # True for a backend that keys the inbox on a session cookie. On one shared
    # cookie jar, every generate() then returns the same address and two callers
    # read each other's mail. Set, the transport runs each request on a
    # throwaway session and the adapter replays the identity from Address.state.
    fresh_session: bool = False

    def serves(self, kind: Kind) -> bool:
        return kind in self.kind


@dataclass(frozen=True, slots=True)
class Address:
    value: str
    provider: str
    expires_at: datetime | None = None
    # Opaque per-provider state. Core never inspects it, never parses it, and
    # never logs it; only the issuing provider reads it back. It is a plain
    # string rather than a structured field because providers need different
    # things — mail.tm must re-mint an expiring JWT and so stores address,
    # password and token together as JSON; Guerrilla stores one sid_token.
    # Giving core a schema for this would mean giving it 60 schemas.
    state: str | None = field(default=None, repr=False)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at


@dataclass(frozen=True, slots=True)
class MessageRow:
    id: str
    sender: str
    subject: str
    received_at: datetime | None
    locked: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    sender: str
    subject: str
    received_at: datetime | None
    html: str
    text: str
    links: tuple[str, ...] = ()
