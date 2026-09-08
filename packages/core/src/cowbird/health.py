from __future__ import annotations

import json
import os
import statistics
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from cowbird.errors import CloudflareChallenge, SchemaDrift

# How far past its own trailing baseline a provider may drift before it is
# ranked last. Per-provider, not absolute: emailnator's 39-second first-body
# read is normal for emailnator and pathological for mail.tm.
#
# SLOW means "slower than this provider's own recent baseline", not slow in
# any absolute sense. It is deliberately self-clearing: if a provider settles
# into a new, sustained pace, the trailing median rises with it and status
# returns to OK once the new pace stops looking like a regression. Absolute
# slowness is the pool's job via p50 ranking, not this store's job via
# status. Replacing this with an absolute threshold would be a regression —
# it would permanently mark down a provider whose normal pace is just slow.
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
        # A success through the current egress is proof the residential-IP
        # hint no longer applies.
        entry.needs_residential_ip = False
        entry.status = (
            Status.SLOW if baseline and seconds > baseline * SLOW_FACTOR else Status.OK
        )

    def record_failure(self, provider: str, exc: Exception) -> None:
        entry = self._entry(provider)
        entry.last_checked = datetime.now(UTC)
        entry.last_failure = repr(exc)
        if isinstance(exc, CloudflareChallenge):
            entry.needs_residential_ip = True
        # Quarantine means an adapter is wrong. A later, unrelated failure is
        # not a verdict on the adapter, so it must not downgrade or overwrite
        # QUARANTINED — only a human clears it.
        if entry.status is Status.QUARANTINED:
            return
        entry.status = Status.QUARANTINED if isinstance(exc, SchemaDrift) else Status.DOWN

    def status(self, provider: str) -> Status:
        return self._entry(provider).status

    def p50(self, provider: str) -> float | None:
        latencies = self._entry(provider).latencies
        return statistics.median(latencies) if latencies else None

    def snapshot(self) -> dict[str, ProviderHealth]:
        # Copy each entry (and its mutable latencies deque) so callers can't
        # rewrite live health state through the value they were handed.
        return {
            provider: replace(entry, latencies=deque(entry.latencies, maxlen=_WINDOW))
            for provider, entry in self._entries.items()
        }

    def to_dict(self) -> dict[str, dict]:
        return {
            name: {
                "status": str(entry.status),
                "latencies": list(entry.latencies),
                "last_checked": (
                    entry.last_checked.isoformat() if entry.last_checked else None
                ),
                "last_failure": entry.last_failure,
                "needs_residential_ip": entry.needs_residential_ip,
            }
            for name, entry in self._entries.items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, dict]) -> HealthStore:
        store = cls()
        for name, raw in data.items():
            entry = store._entry(name)
            try:
                entry.status = Status(raw["status"])
            except (KeyError, ValueError):
                # Invalid status degrades entire load to empty store.
                return cls()
            # Validate latencies: must be list of numbers. A malformed value
            # like latencies: "[1,2,3]" (string) would iterate as chars,
            # poisoning p50() and breaking routing. Reject non-numbers and bools
            # (isinstance(True, int) is True in Python).
            raw_latencies = raw.get("latencies", [])
            if isinstance(raw_latencies, list):
                entry.latencies.extend(
                    v
                    for v in raw_latencies
                    if isinstance(v, int | float) and not isinstance(v, bool)
                )
            checked = raw.get("last_checked")
            entry.last_checked = datetime.fromisoformat(checked) if checked else None
            entry.last_failure = raw.get("last_failure")
            entry.needs_residential_ip = bool(raw.get("needs_residential_ip", False))
        return store

    def save(self, path: str | Path) -> None:
        """Write atomically. A half-written health file read by the next run
        would be worse than no file at all."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: str | Path) -> HealthStore:
        """Never raises. A missing or corrupt cache degrades to an empty store —
        losing health history is an inconvenience, refusing to start is not."""
        path = Path(path)
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return cls()
