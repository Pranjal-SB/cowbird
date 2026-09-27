"""Behaviour that only appears once several providers are installed.

These use the real entry-point registry rather than a hand-built fake, so they
exercise whatever is actually installed in the workspace. Every provider until
now has been tested alone; nothing proved the pool routes across them, fails
over when one dies, or spreads a batch. Those are the behaviours the whole
architecture exists for.

No network: `Pool.candidates` is pure policy, so routing is testable offline.
"""

from __future__ import annotations

import pytest
from cowbird.health import HealthStore, Status
from cowbird.models import Kind
from cowbird.pool import Pool, Request
from cowbird.registry import Registry
from cowbird.testing import FakeSolver


def real_pool(solver=None) -> tuple[Pool, HealthStore]:
    health = HealthStore()
    # One store shared by registry and pool, matching default_pool() in
    # production. Wiring them to separate stores would test a configuration
    # that does not exist.
    return Pool(Registry(health=health, solver=solver), health), health


def installed() -> list[str]:
    return sorted(p.name for p in Registry().all())


def test_more_than_one_provider_is_installed():
    names = installed()
    assert len(names) >= 2, f"expected a fleet, found {names}"


def test_every_installed_provider_declares_a_coherent_capability_record():
    for provider in Registry().all():
        caps = provider.caps
        assert caps.kind, f"{provider.name} serves no kind"
        assert caps.sites, f"{provider.name} declares no sites"
        assert caps.max_concurrency >= 1
        assert caps.poll_interval > 0
        if caps.domains:
            assert caps.domain_count >= len(caps.domains)


def test_provider_names_are_unique():
    names = [p.name for p in Registry().all()]
    assert len(names) == len(set(names))


def test_a_down_provider_is_skipped_in_favour_of_a_healthy_one():
    pool, health = real_pool()
    first = installed()[0]
    health._entry(first).status = Status.DOWN
    routed = [p.name for p in pool.candidates(Request())]
    assert first not in routed
    assert routed, "the whole fleet went unroutable when one provider went down"


def test_a_quarantined_provider_is_never_routed_to():
    pool, health = real_pool()
    first = installed()[0]
    health._entry(first).status = Status.QUARANTINED
    assert first not in [p.name for p in pool.candidates(Request())]


def test_a_slow_provider_still_routes_but_ranks_behind_a_healthy_one():
    # SLOW is deliberately routable: a slow provider beats no provider. It must
    # rank last among routable ones, not drop out.
    pool, health = real_pool()
    names = installed()
    health._entry(names[0]).status = Status.SLOW
    routed = [p.name for p in pool.candidates(Request())]
    assert names[0] in routed
    assert routed[-1] == names[0]


def test_gmail_alias_requests_only_reach_gmail_alias_providers():
    pool, _ = real_pool()
    routed = pool.candidates(Request(kind=Kind.GMAIL_ALIAS))
    # Assert the filter has something to bite on. A bare `for ... assert` loop
    # passes vacuously when nothing serves the kind, which is exactly what
    # happened while SmailPro was parked -- green, and testing nothing.
    assert routed, "no installed provider serves GMAIL_ALIAS; this test proves nothing"
    for provider in routed:
        assert provider.caps.serves(Kind.GMAIL_ALIAS)


def test_a_custom_local_part_request_only_reaches_providers_that_allow_one():
    pool, _ = real_pool()
    routed = pool.candidates(Request(local="chosen"))
    assert routed, "no installed provider accepts a custom local-part"
    for provider in routed:
        assert provider.caps.custom_local


def test_a_domain_filter_only_excludes_providers_that_declare_their_domains():
    # Providers that discover domains at runtime declare `domains=()`, and the
    # pool cannot pre-filter those -- so they stay candidates and answer with
    # NotSupported at generate() time instead. Only a provider with a declared
    # list can be ruled out up front. Filtering the unknowns out here would
    # preferentially drop exactly the providers with the widest domain coverage.
    pool, _ = real_pool()
    routed = pool.candidates(Request(domain="not-a-real-domain.invalid"))
    for provider in routed:
        assert not provider.caps.domains, (
            f"{provider.name} declares domains but was not filtered on one it lacks"
        )


async def test_a_declared_domain_provider_refuses_a_domain_it_does_not_serve():
    # The other half of the contract above: what the pool cannot decide, the
    # provider must. NotSupported is an answer to the caller, so it must not
    # be reroutable -- rerouting would walk the whole fleet for a domain that
    # by definition none of them serve.
    from cowbird.errors import NotSupported
    from cowbird.provider import GenerateOptions

    declared = [p for p in Registry().all() if p.caps.domains]
    assert declared, "no installed provider declares a domain list"
    for provider in declared:
        with pytest.raises(NotSupported) as caught:
            await provider.generate(GenerateOptions(domain="not-a-real-domain.invalid"))
        assert caught.value.reroutable is False


def test_naming_a_provider_bypasses_ranking_and_returns_exactly_it():
    pool, _ = real_pool()
    for name in installed():
        assert [p.name for p in pool.candidates(Request(provider=name))] == [name]


def test_the_whole_fleet_being_down_leaves_no_candidates():
    # The caller-facing failure is NoProviderAvailable from acquire(); candidates
    # simply empties. Asserting it here keeps the two behaviours distinguishable.
    pool, health = real_pool()
    for name in installed():
        health._entry(name).status = Status.DOWN
    assert pool.candidates(Request()) == []


@pytest.mark.parametrize("name", installed())
def test_every_provider_is_individually_routable_when_healthy(name):
    # With a solver, so a needs_solver provider is routable too; without
    # one the pool skips it by design.
    pool, _ = real_pool(solver=FakeSolver())
    assert name in [p.name for p in pool.candidates(Request())]
    pool, _ = real_pool()
    routed = name in [p.name for p in pool.candidates(Request())]
    assert routed is not Registry().get(name).caps.needs_solver
