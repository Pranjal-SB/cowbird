# packages/core/src/cowbird/pool.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from cowbird.errors import CowbirdError, NoProviderAvailable
from cowbird.health import ROUTABLE, HealthStore
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions, Provider
from cowbird.registry import Registry


@dataclass(frozen=True, slots=True)
class Request:
    provider: str | None = None
    kind: Kind | None = None
    local: str | None = None
    domain: str | None = None
    domain_not_in: tuple[str, ...] = ()
    address_ttl: timedelta | None = None
    message_ttl: timedelta | None = None
    push: bool | None = None
    delete: bool | None = None
    self_hosted: bool | None = None

    def to_options(self) -> GenerateOptions:
        return GenerateOptions(kind=self.kind, local=self.local, domain=self.domain)


class Pool:
    """Chooses a provider. Pure policy — it performs no I/O of its own, so the
    whole routing decision is testable without a network."""

    def __init__(self, registry: Registry, health: HealthStore) -> None:
        self.registry = registry
        self.health = health

    def candidates(self, req: Request) -> list[Provider]:
        if req.provider is not None:
            return [self.registry.get(req.provider)]
        eligible = [p for p in self.registry.all() if self._matches(p, req)]
        return sorted(eligible, key=lambda p: self._rank(p))

    def _matches(self, provider: Provider, req: Request) -> bool:
        caps = provider.caps
        if self.health.status(provider.name) not in ROUTABLE:
            return False
        if caps.needs_solver and self.registry.solver is None:
            return False
        if req.kind is not None and not caps.serves(req.kind):
            return False
        if req.push is not None and caps.push != req.push:
            return False
        if req.delete is not None and caps.delete != req.delete:
            return False
        if req.self_hosted is not None and caps.self_hosted != req.self_hosted:
            return False
        if req.local is not None and not caps.custom_local:
            return False
        if req.domain is not None and caps.domains and req.domain not in caps.domains:
            return False
        if req.domain_not_in and caps.domains and all(d in req.domain_not_in for d in caps.domains):
            return False
        if not self._outlives(caps.address_ttl, req.address_ttl):
            return False
        return self._outlives(caps.message_ttl, req.message_ttl)

    @staticmethod
    def _outlives(have: timedelta | None, need: timedelta | None) -> bool:
        if need is None:
            return True
        return have is None or have >= need

    def _rank(self, provider: Provider) -> tuple[int, float, int, str]:
        status = self.health.status(provider.name)
        return (
            ROUTABLE.index(status),
            self.health.p50(provider.name) or 0.0,
            -provider.caps.domain_count,
            provider.name,
        )

    async def acquire(self, req: Request) -> tuple[Provider, Address]:
        tried: list[str] = []
        last: Exception | None = None
        for provider in self.candidates(req):
            tried.append(provider.name)
            try:
                address = await provider.generate(req.to_options())
            except CowbirdError as exc:
                # HealthTracked (the registry wrapper) already recorded this
                # failure. Recording it again here would double-count it.
                if not exc.reroutable:
                    raise
                last = exc
                continue
            if self._domain_blocked(address, req):
                continue
            # HealthTracked already recorded the success. It is the single
            # chokepoint that covers generate/list/get/delete alike; Pool only
            # ever calls generate, so recording here too would just double it.
            return provider, address
        raise NoProviderAvailable(tried=tried, last=last)

    @staticmethod
    def _domain_blocked(address: Address, req: Request) -> bool:
        if not req.domain_not_in:
            return False
        domain = address.value.rsplit("@", 1)[-1].lower()
        return domain in {d.lower() for d in req.domain_not_in}
