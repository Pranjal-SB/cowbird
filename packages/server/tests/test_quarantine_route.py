import os

import pytest
from cowbird.errors import CloudflareChallenge, SchemaDrift
from cowbird.health import Status
from cowbird_server import db

_HAS_PG = bool(os.environ.get("DATABASE_URL"))


def test_clearing_a_quarantine_needs_a_key(client):
    assert client.delete("/v1/providers/fake/quarantine").status_code == 401


def test_clearing_an_unquarantined_provider_is_not_an_error(client, auth):
    # Idempotent on purpose: an operator retrying a clear should not get a 404
    # that reads as "no such provider".
    response = client.delete("/v1/providers/fake/quarantine", headers=auth)
    assert response.status_code == 200
    assert response.json()["data"] == {"cleared": False}


def test_clearing_an_unknown_provider_is_a_404(client, auth):
    assert client.delete("/v1/providers/nope/quarantine", headers=auth).status_code == 404


def test_clearing_returns_the_provider_to_routing(client, auth):
    pool = client.app.state.pool
    pool.health.record_failure("fake", SchemaDrift("fake", expected="a", got="b"))
    assert pool.health.status("fake") is Status.QUARANTINED

    response = client.delete("/v1/providers/fake/quarantine", headers=auth)
    assert response.json()["data"] == {"cleared": True}
    assert pool.health.status("fake") is Status.OK


def test_clearing_keeps_the_latency_history_and_the_residential_hint(client, auth):
    # restore() took every other field from defaults, so a clear reported
    # p50: null, dropped the window SLOW detection and pool ranking run on, and
    # threw away a hint that may be suppressing a known-blocked egress.
    pool = client.app.state.pool
    for _ in range(6):
        pool.health.record_success("fake", "list", 0.4)
    pool.health.record_failure("fake", CloudflareChallenge("challenged"))
    pool.health.record_failure("fake", SchemaDrift("fake", expected="a", got="b"))
    assert pool.health.status("fake") is Status.QUARANTINED

    client.delete("/v1/providers/fake/quarantine", headers=auth)
    assert pool.health.status("fake") is Status.OK
    assert pool.health.p50("fake") == 0.4
    assert pool.health.snapshot()["fake"].needs_residential_ip is True


@pytest.mark.pg
@pytest.mark.skipif(not _HAS_PG, reason="no DATABASE_URL")
async def test_a_cleared_quarantine_does_not_come_back_from_the_other_instance(pg_pool):
    # The sequence a single DELETE used to lose. Both instances hold the
    # quarantine in memory; only one serves the clear; the other's next flush
    # used to re-insert the global row and put every instance back where it
    # started about four seconds after a response that said cleared: true.
    from cowbird.health import HealthStore
    from cowbird_server.healthsync import HealthSync

    pool = await pg_pool()
    try:
        await db.migrate(pool)
        async with pool.acquire() as conn:
            await conn.execute("truncate provider_health, provider_quarantine")

        one, two = HealthStore(), HealthStore()
        one.record_failure("fake", SchemaDrift("fake", expected="a", got="b"))
        sync_one = HealthSync(pool, one, "backend-1")
        sync_two = HealthSync(pool, two, "backend-2")
        await sync_one.flush()
        await sync_two.flush()
        assert two.status("fake") is Status.QUARANTINED

        # What the DELETE route does: drop the row, clear it on this instance.
        async with pool.acquire() as conn:
            await conn.execute("delete from provider_quarantine where provider = 'fake'")
        one.unquarantine("fake")

        for _ in range(3):
            await sync_two.flush()
            await sync_one.flush()

        async with pool.acquire() as conn:
            assert await conn.fetchval("select count(*) from provider_quarantine") == 0
        assert one.status("fake") is not Status.QUARANTINED
        assert two.status("fake") is not Status.QUARANTINED
    finally:
        await pool.close()
