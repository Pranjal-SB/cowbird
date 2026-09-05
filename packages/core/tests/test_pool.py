# packages/core/tests/test_pool.py
from dataclasses import replace
from datetime import timedelta

import pytest
from cowbird.errors import MessageLocked, NoProviderAvailable, ProviderDown
from cowbird.health import HealthStore, Status
from cowbird.models import Address, Kind
from cowbird.pool import Pool, Request
from cowbird.registry import Registry
from cowbird.testing import CAPS, FakeProvider


def provider_class(name, caps=CAPS, fails=None, returns=None):
    class P(FakeProvider):
        pass

    P.name = name
    P.caps = caps
    if fails is not None:

        async def generate(self, opts=None):
            raise fails

        P.generate = generate
    elif returns is not None:

        async def generate(self, opts=None, _address=returns):
            return _address

        P.generate = generate
    return P


def build(*classes, health=None):
    reg = Registry(transport_factory=lambda name: None, discover=False)
    for cls in classes:
        reg.register(cls)
    return Pool(reg, health or HealthStore())


def test_candidates_exclude_providers_lacking_the_requested_kind():
    gmail = provider_class("g", replace(CAPS, kind=frozenset({Kind.GMAIL_ALIAS})))
    pool = build(gmail, provider_class("d"))
    names = [p.name for p in pool.candidates(Request(kind=Kind.GMAIL_ALIAS))]
    assert names == ["g"]


def test_candidates_exclude_providers_that_are_not_routable():
    health = HealthStore()
    health._entry("bad").status = Status.DOWN
    pool = build(provider_class("bad"), provider_class("good"), health=health)
    assert [p.name for p in pool.candidates(Request())] == ["good"]


def test_candidates_exclude_providers_serving_a_blocked_domain_only():
    blocked = provider_class("b", replace(CAPS, domains=("spam.test",)))
    ok = provider_class("o", replace(CAPS, domains=("clean.test",)))
    pool = build(blocked, ok)
    names = [p.name for p in pool.candidates(Request(domain_not_in=("spam.test",)))]
    assert names == ["o"]


def test_candidates_exclude_providers_whose_address_expires_too_soon():
    short = provider_class("s", replace(CAPS, address_ttl=timedelta(minutes=10)))
    long = provider_class("l", replace(CAPS, address_ttl=None))
    pool = build(short, long)
    names = [p.name for p in pool.candidates(Request(address_ttl=timedelta(hours=1)))]
    assert names == ["l"]


def test_healthy_providers_outrank_slow_ones():
    health = HealthStore()
    health._entry("slow").status = Status.SLOW
    pool = build(provider_class("slow"), provider_class("fast"), health=health)
    assert [p.name for p in pool.candidates(Request())] == ["fast", "slow"]


def test_within_a_status_the_lower_median_latency_wins():
    health = HealthStore()
    for _ in range(5):
        health.record_success("quick", "list", 0.2)
        health.record_success("sluggish", "list", 4.0)
    pool = build(provider_class("quick"), provider_class("sluggish"), health=health)
    assert [p.name for p in pool.candidates(Request())] == ["quick", "sluggish"]


def test_more_domains_breaks_the_tie():
    few = provider_class("few", replace(CAPS, domain_count=1))
    many = provider_class("many", replace(CAPS, domain_count=50))
    pool = build(few, many)
    assert [p.name for p in pool.candidates(Request())] == ["many", "few"]


def test_pinning_a_provider_skips_routing_entirely():
    pool = build(provider_class("a"), provider_class("b"))
    assert [p.name for p in pool.candidates(Request(provider="b"))] == ["b"]


async def test_acquire_reroutes_past_a_reroutable_failure():
    pool = build(provider_class("broken", fails=ProviderDown("x")), provider_class("works"))
    provider, address = await pool.acquire(Request())
    assert provider.name == "works"
    assert isinstance(address, Address)


async def test_acquire_marks_the_failing_provider_down():
    health = HealthStore()
    pool = build(
        provider_class("broken", fails=ProviderDown("x")),
        provider_class("works"),
        health=health,
    )
    await pool.acquire(Request())
    assert health.status("broken") is Status.DOWN


async def test_acquire_does_not_reroute_past_a_caller_facing_failure():
    pool = build(provider_class("locked", fails=MessageLocked("paywall")), provider_class("b"))
    with pytest.raises(MessageLocked):
        await pool.acquire(Request(provider="locked"))


async def test_acquire_raises_when_everything_failed_and_says_what_it_tried():
    pool = build(provider_class("a", fails=ProviderDown("x")))
    with pytest.raises(NoProviderAvailable) as excinfo:
        await pool.acquire(Request())
    assert excinfo.value.tried == ["a"]


def _mixed_provider(name="mixed"):
    return provider_class(
        name,
        replace(CAPS, domains=("clean.test", "spam.test")),
        returns=Address(value="a@spam.test", provider=name),
    )


def test_candidates_stays_permissive_for_a_provider_with_a_mixed_domain_set():
    pool = build(_mixed_provider(), provider_class("clean"))
    names = {p.name for p in pool.candidates(Request(domain_not_in=("spam.test",)))}
    assert "mixed" in names


async def test_acquire_skips_a_blocked_address_and_returns_the_clean_one():
    pool = build(_mixed_provider(), provider_class("clean"))
    provider, address = await pool.acquire(Request(domain_not_in=("spam.test",)))
    assert provider.name == "clean"
    assert address.value.endswith("@fake.test")


async def test_acquire_raises_when_only_a_blocked_address_is_available():
    pool = build(_mixed_provider())
    with pytest.raises(NoProviderAvailable) as excinfo:
        await pool.acquire(Request(domain_not_in=("spam.test",)))
    assert excinfo.value.tried == ["mixed"]


async def test_rejecting_a_blocked_domain_does_not_mark_the_provider_unhealthy():
    health = HealthStore()
    pool = build(_mixed_provider(), provider_class("clean"), health=health)
    await pool.acquire(Request(domain_not_in=("spam.test",)))
    assert health.status("mixed") is Status.OK


async def test_blocked_domain_match_is_case_insensitive():
    provider = provider_class(
        "shouty",
        replace(CAPS, domains=("clean.test", "Spam.Test")),
        returns=Address(value="User@Spam.Test", provider="shouty"),
    )
    pool = build(provider, provider_class("clean"))
    result_provider, _ = await pool.acquire(Request(domain_not_in=("spam.test",)))
    assert result_provider.name == "clean"
