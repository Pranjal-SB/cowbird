from cowbird.errors import CloudflareChallenge, ProviderDown, SchemaDrift
from cowbird.health import ROUTABLE, HealthStore, Status


def test_unknown_provider_is_assumed_ok_until_proven_otherwise():
    # A provider with no history must be routable, or a cold start routes nowhere.
    assert HealthStore().status("new") is Status.OK


def test_a_failure_marks_the_provider_down():
    store = HealthStore()
    store.record_failure("p", ProviderDown("boom"))
    assert store.status("p") is Status.DOWN


def test_schema_drift_quarantines_rather_than_marking_down():
    store = HealthStore()
    store.record_failure("p", SchemaDrift("p", expected="a", got="b"))
    assert store.status("p") is Status.QUARANTINED


def test_a_cloudflare_challenge_does_not_quarantine():
    # From a datacenter IP this usually means the egress is wrong, not the provider.
    store = HealthStore()
    store.record_failure("p", CloudflareChallenge("challenged"))
    assert store.status("p") is Status.DOWN
    assert store.snapshot()["p"].needs_residential_ip is True


def test_success_after_failure_restores_the_provider():
    store = HealthStore()
    store.record_failure("p", ProviderDown("boom"))
    store.record_success("p", "list", 0.2)
    assert store.status("p") is Status.OK


def test_quarantine_is_not_cleared_by_a_success():
    # Schema drift needs a human and an adapter change, not a lucky retry.
    store = HealthStore()
    store.record_failure("p", SchemaDrift("p", expected="a", got="b"))
    store.record_success("p", "list", 0.2)
    assert store.status("p") is Status.QUARANTINED


def test_latency_regression_past_three_times_baseline_is_slow():
    store = HealthStore()
    for _ in range(10):
        store.record_success("p", "list", 1.0)
    store.record_success("p", "list", 30.0)
    assert store.status("p") is Status.SLOW


def test_slow_is_still_routable_because_slow_beats_nothing():
    assert Status.SLOW in ROUTABLE
    assert Status.DOWN not in ROUTABLE
    assert Status.QUARANTINED not in ROUTABLE
