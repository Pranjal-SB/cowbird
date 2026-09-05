from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from cowbird.errors import CloudflareChallenge, SchemaDrift

# How far past its own trailing baseline a provider may drift before it is
# ranked last. Per-provider, not absolute: emailnator's 39-second first-body
# read is normal for emailnator and pathological for mail.tm.
SLOW_FACTOR = 3.0
_WINDOW = 20


class Status(StrEnum):
    OK = "ok"
    SLOW = "slow"
    DOWN = "down"
    QUARANTINED = "quarantined"


ROUTABLE: tuple[Status, ...] = (Status.OK, Status.SLOW)


@dataclass
class ProviderHealth:
    status: Status = Status.OK
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=_WINDOW))
    last_checked: datetime | None = None
    last_failure: str | None = None
    needs_residential_ip: bool = False


class HealthStore:
    """What the pool knows about which providers still work.

    In-memory here. The server persists it and feeds it from the scheduled live
    canary; this interface does not change.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ProviderHealth] = {}

    def _entry(self, provider: str) -> ProviderHealth:
        return self._entries.setdefault(provider, ProviderHealth())

    def record_success(self, provider: str, op: str, seconds: float) -> None:
        entry = self._entry(provider)
        entry.last_checked = datetime.now(UTC)
        # Quarantine means an adapter is wrong. One passing call does not make
        # it right, so only a human clears it.
        if entry.status is Status.QUARANTINED:
            return
        baseline = statistics.median(entry.latencies) if len(entry.latencies) >= 5 else None
        entry.latencies.append(seconds)
        entry.last_failure = None
        entry.status = (
            Status.SLOW if baseline and seconds > baseline * SLOW_FACTOR else Status.OK
        )

    def record_failure(self, provider: str, exc: Exception) -> None:
        entry = self._entry(provider)
        entry.last_checked = datetime.now(UTC)
        entry.last_failure = repr(exc)
        if isinstance(exc, CloudflareChallenge):
            entry.needs_residential_ip = True
        entry.status = Status.QUARANTINED if isinstance(exc, SchemaDrift) else Status.DOWN

    def status(self, provider: str) -> Status:
        return self._entry(provider).status

    def p50(self, provider: str) -> float | None:
        latencies = self._entry(provider).latencies
        return statistics.median(latencies) if latencies else None

    def snapshot(self) -> dict[str, ProviderHealth]:
        return dict(self._entries)
