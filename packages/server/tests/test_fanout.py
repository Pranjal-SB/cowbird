"""The tests that justify the whole shared-state design.

Needs the compose stack up:

    docker compose up -d --build
    COWBIRD_STACK=1 uv run pytest packages/server/tests/test_fanout.py -v
"""

import os
import time

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COWBIRD_STACK"), reason="compose stack not running"
    ),
]

ONE = "http://127.0.0.1:8001"
TWO = "http://127.0.0.1:8002"
PROXY = "http://127.0.0.1:8000"
AUTH = {"x-api-key": "local-dev"}
DSN = "postgresql://cowbird:cowbird@127.0.0.1:5432/cowbird"


def test_an_address_issued_by_one_instance_is_readable_from_the_other():
    # The claim the Store seam exists for. Before shared state this returned a
    # 404 from the second instance, which is roughly half of all reads once a
    # load balancer is in front.
    #
    # inboxes is used deliberately: its generate() makes no HTTP call, so
    # acquiring is free and deterministic. The read does reach inboxes.com.
    created = httpx.post(
        f"{ONE}/v1/inboxes", headers=AUTH, json={"provider": "inboxes"}, timeout=30
    )
    assert created.status_code == 200
    address = created.json()["data"]["address"]

    read = httpx.get(f"{TWO}/v1/inboxes/{address}/messages", headers=AUTH, timeout=30)
    assert read.status_code == 200, read.text


def test_the_proxy_routes_to_both_instances():
    # The proxy is not used for the cross-instance assertion above: round robin
    # makes "the other instance" a coin flip, and that test would pass half the
    # time for the wrong reason. This is what the proxy is actually checked for.
    for _ in range(4):
        assert httpx.get(f"{PROXY}/health", timeout=10).json()["data"]["status"] == "ok"


async def test_a_quarantine_raised_on_one_instance_reaches_the_other():
    # Seeded straight into the table rather than by provoking a real drift:
    # what is under test is propagation between instances, and making a live
    # backend return malformed JSON is not something a test can arrange.
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("delete from provider_quarantine where provider = 'inboxes'")
        await conn.execute(
            "insert into provider_quarantine (provider, reason, instance_id)"
            " values ('inboxes', 'seeded by test', 'backend-1')"
        )
    finally:
        await conn.close()

    # HEALTH_FLUSH_SECONDS is 2 in compose, so two intervals plus slack is
    # enough without making the test slow.
    time.sleep(6)

    listed = httpx.get(f"{TWO}/v1/providers", headers=AUTH, timeout=10).json()["data"]
    entry = next(p for p in listed if p["provider"] == "inboxes")
    assert entry["status"] == "quarantined"

    # Leave the stack usable for a re-run. Without this the next run starts with
    # inboxes quarantined and the address test above fails for an unrelated
    # reason, which is a confusing way to learn that a test did not clean up.
    httpx.delete(f"{TWO}/v1/providers/inboxes/quarantine", headers=AUTH, timeout=10)
    httpx.delete(f"{ONE}/v1/providers/inboxes/quarantine", headers=AUTH, timeout=10)
