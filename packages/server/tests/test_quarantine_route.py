import os

import pytest
from cowbird.errors import SchemaDrift
from cowbird.health import Status

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


@pytest.mark.pg
@pytest.mark.skipif(not _HAS_PG, reason="no DATABASE_URL")
async def test_clearing_deletes_the_global_row_so_it_does_not_come_back():
    # Without the delete, the next flush would read the row and re-quarantine
    # the provider, and the clear would look like it silently failed.
    from cowbird.health import HealthStore
    from cowbird_server import db
    from cowbird_server.healthsync import HealthSync

    pool = await db.connect(os.environ["DATABASE_URL"])
    await db.migrate(pool)
    async with pool.acquire() as conn:
        await conn.execute("truncate provider_health, provider_quarantine")
        await conn.execute(
            "insert into provider_quarantine (provider, reason, instance_id)"
            " values ('fake', 'drift', 'backend-1')"
        )
        await conn.execute("delete from provider_quarantine where provider = $1", "fake")

    health = HealthStore()
    await HealthSync(pool, health, "backend-2").load()
    assert health.status("fake") is not Status.QUARANTINED
    await pool.close()
