import json
from datetime import datetime

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


def test_mutating_a_snapshot_does_not_affect_the_live_store():
    store = HealthStore()
    store.record_success("p", "list", 1.0)
    snap = store.snapshot()
    snap["p"].status = Status.DOWN
    snap["p"].latencies.append(999.0)
    assert store.status("p") is Status.OK
    assert store.p50("p") == 1.0


def test_quarantine_survives_a_later_unrelated_failure_and_success():
    store = HealthStore()
    store.record_failure("p", SchemaDrift("p", expected="a", got="b"))
    assert store.status("p") is Status.QUARANTINED
    store.record_failure("p", ProviderDown("blip"))
    assert store.status("p") is Status.QUARANTINED
    store.record_success("p", "list", 0.2)
    assert store.status("p") is Status.QUARANTINED
    assert "blip" in store.snapshot()["p"].last_failure


def test_cloudflare_challenge_while_quarantined_still_sets_residential_flag():
    store = HealthStore()
    store.record_failure("p", SchemaDrift("p", expected="a", got="b"))
    store.record_failure("p", CloudflareChallenge("challenged"))
    assert store.status("p") is Status.QUARANTINED
    assert store.snapshot()["p"].needs_residential_ip is True


def test_last_failure_updates_while_quarantined():
    store = HealthStore()
    store.record_failure("p", SchemaDrift("p", expected="a", got="b"))
    store.record_failure("p", ProviderDown("second failure"))
    assert "second failure" in store.snapshot()["p"].last_failure


def test_round_trip_preserves_status_latencies_and_flags(tmp_path):
    store = HealthStore()
    for _ in range(6):
        store.record_success("fast", "list", 0.2)
    store.record_failure("broken", SchemaDrift("broken", expected="a", got="b"))
    store.record_failure("blocked", CloudflareChallenge("challenged"))

    path = tmp_path / "health.json"
    store.save(path)
    back = HealthStore.load(path)

    assert back.status("fast") is Status.OK
    assert back.p50("fast") == 0.2
    assert back.status("broken") is Status.QUARANTINED
    assert back.status("blocked") is Status.DOWN
    assert back.snapshot()["blocked"].needs_residential_ip is True


def test_quarantine_survives_a_restart(tmp_path):
    # The whole point of persistence: a human must clear a quarantine, and a
    # process restart is not a human.
    store = HealthStore()
    store.record_failure("p", SchemaDrift("p", expected="a", got="b"))
    path = tmp_path / "health.json"
    store.save(path)

    back = HealthStore.load(path)
    back.record_success("p", "list", 0.1)
    assert back.status("p") is Status.QUARANTINED


def test_load_of_a_missing_file_is_an_empty_store(tmp_path):
    store = HealthStore.load(tmp_path / "nope.json")
    assert store.snapshot() == {}


def test_load_of_a_corrupt_file_does_not_raise(tmp_path):
    # A mangled cache file must never stop the CLI from working.
    path = tmp_path / "health.json"
    path.write_text("{not json at all")
    store = HealthStore.load(path)
    assert store.snapshot() == {}


def test_save_is_atomic_leaving_no_partial_file(tmp_path):
    path = tmp_path / "health.json"
    store = HealthStore()
    store.record_success("p", "list", 1.0)
    store.save(path)
    assert json.loads(path.read_text())["p"]["status"] == "ok"
    assert list(tmp_path.iterdir()) == [path]


def test_last_checked_survives_the_round_trip(tmp_path):
    store = HealthStore()
    store.record_success("p", "list", 1.0)
    path = tmp_path / "health.json"
    store.save(path)
    restored = HealthStore.load(path).snapshot()["p"].last_checked
    assert isinstance(restored, datetime)
    assert restored.tzinfo is not None


def test_malformed_latencies_string_loads_to_empty_window(tmp_path):
    # String latencies like "[1,2,3]" iterate char-by-char, poisoning p50().
    # Must load to empty window and p50() returns None.
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps({"p": {"status": "ok", "latencies": "[1,2,3]"}})
    )
    store = HealthStore.load(path)
    assert store.p50("p") is None
    assert list(store.snapshot()["p"].latencies) == []


def test_latencies_with_mixed_valid_and_junk_keeps_only_numbers(tmp_path):
    # [1.0, "x", None, 2.0, True] should keep only 1.0 and 2.0. Bools
    # are rejected even though isinstance(True, int) is True.
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "p": {
                    "status": "ok",
                    "latencies": [1.0, "x", None, 2.0, True],
                }
            }
        )
    )
    store = HealthStore.load(path)
    assert list(store.snapshot()["p"].latencies) == [1.0, 2.0]
    assert store.p50("p") == 1.5


def test_invalid_status_degrades_entire_load_to_empty_store(tmp_path):
    # An invalid status value should return empty store, not raise.
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"p": {"status": "invalid"}}))
    store = HealthStore.load(path)
    assert store.snapshot() == {}
