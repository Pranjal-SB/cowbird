import os

import pytest
from cowbird.errors import CloudflareChallenge, SchemaDrift
from cowbird.health import HealthStore, Status
from cowbird_server import db
from cowbird_server.healthsync import HealthSync

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="no DATABASE_URL"),
]


@pytest.fixture
async def pool(pg_pool):
    p = await pg_pool()
    await db.migrate(p)
    async with p.acquire() as conn:
        await conn.execute("truncate provider_health, provider_quarantine")
    yield p
    await p.close()


async def test_an_instance_finds_its_own_latency_history_after_a_restart(pool):
    first = HealthStore()
    for _ in range(6):
        first.record_success("mailtm", "list", 0.4)
    await HealthSync(pool, first, "backend-1").flush()

    restarted = HealthStore()
    await HealthSync(pool, restarted, "backend-1").load()
    assert restarted.p50("mailtm") == 0.4


async def test_a_restarted_instance_does_not_come_back_down(pool):
    first = HealthStore()
    first.record_failure("mailtm", CloudflareChallenge("challenged"))
    assert first.status("mailtm") is Status.DOWN
    await HealthSync(pool, first, "backend-1").flush()

    restarted = HealthStore()
    await HealthSync(pool, restarted, "backend-1").load()
    assert restarted.status("mailtm") is Status.OK
    # The residential hint is a different kind of fact and does survive.
    assert restarted.snapshot()["mailtm"].needs_residential_ip is True


async def test_a_quarantine_reaches_the_other_instance(pool):
    a = HealthStore()
    a.record_failure("mailtm", SchemaDrift("mailtm", expected="a", got="b"))
    await HealthSync(pool, a, "backend-1").flush()

    b = HealthStore()
    sync_b = HealthSync(pool, b, "backend-2")
    await sync_b.flush()
    assert b.status("mailtm") is Status.QUARANTINED


async def test_a_residential_ip_hint_does_not_reach_the_other_instance(pool):
    # The test that matters most. Every other test here still passes if the two
    # tables are collapsed into one, and this one does not. Fan-out exists to
    # have several egress IPs: a Cloudflare challenge served to backend-1 says
    # nothing about backend-2, and propagating it would take a reachable
    # provider out of rotation on the other instance.
    a = HealthStore()
    a.record_failure("mailtm", CloudflareChallenge("challenged"))
    await HealthSync(pool, a, "backend-1").flush()

    b = HealthStore()
    await HealthSync(pool, b, "backend-2").load()
    assert b.snapshot().get("mailtm") is None or not b.snapshot()["mailtm"].needs_residential_ip
    assert b.status("mailtm") is not Status.DOWN


async def test_one_instance_does_not_overwrite_anothers_row(pool):
    a = HealthStore()
    a.record_success("mailtm", "list", 0.1)
    await HealthSync(pool, a, "backend-1").flush()

    b = HealthStore()
    b.record_success("mailtm", "list", 9.0)
    await HealthSync(pool, b, "backend-2").flush()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select instance_id, latencies from provider_health where provider = 'mailtm'"
            " order by instance_id"
        )
    assert [r["instance_id"] for r in rows] == ["backend-1", "backend-2"]
    assert rows[0]["latencies"] == [0.1]


async def test_the_first_quarantine_wins_and_later_ones_are_noops(pool):
    a = HealthStore()
    a.record_failure("mailtm", SchemaDrift("mailtm", expected="first", got="x"))
    await HealthSync(pool, a, "backend-1").flush()

    b = HealthStore()
    b.record_failure("mailtm", SchemaDrift("mailtm", expected="second", got="y"))
    await HealthSync(pool, b, "backend-2").flush()

    async with pool.acquire() as conn:
        row = await conn.fetchrow("select instance_id, reason from provider_quarantine")
    assert row["instance_id"] == "backend-1"
    assert "first" in row["reason"]


async def test_a_flush_failure_does_not_raise(pool):
    # Health is a routing heuristic with a working in-memory copy. Taking the
    # server down over a lost flush would turn a cosmetic problem into an outage.
    health = HealthStore()
    health.record_success("mailtm", "list", 0.1)
    sync = HealthSync(pool, health, "backend-1")
    await pool.close()
    await sync.flush()  # must not raise


async def test_the_maintenance_loop_flushes_and_sweeps(pool):
    # sweep() had tests of its own but nothing called it. This pins the wiring,
    # which is the part that was missing: a swept-but-never-scheduled reaper is
    # the same leak with more code.
    import asyncio
    from datetime import UTC, datetime, timedelta

    from cowbird.models import Address
    from cowbird_server import _maintenance
    from cowbird_server.pgstore import PostgresStore

    async with pool.acquire() as conn:
        await conn.execute("truncate addresses")
    store = PostgresStore(pool)
    await store.put(
        Address(
            value="old@fake.test",
            provider="fake",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    health = HealthStore()
    health.record_success("mailtm", "list", 0.2)

    task = asyncio.create_task(
        _maintenance(HealthSync(pool, health, "backend-1"), store, 1)
    )
    await asyncio.sleep(2.5)
    task.cancel()

    async with pool.acquire() as conn:
        assert await conn.fetchval("select count(*) from addresses") == 0
        assert await conn.fetchval(
            "select count(*) from provider_health where instance_id = 'backend-1'"
        ) == 1
