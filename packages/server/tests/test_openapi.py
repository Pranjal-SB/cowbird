from __future__ import annotations

EXPECTED_PATHS = {
    "/health",
    "/v1/providers",
    "/v1/inboxes",
    "/v1/inboxes/{addr}/messages",
    "/v1/inboxes/{addr}/messages/{message_id}",
    "/v1/inboxes/{addr}/wait",
    "/v1/webhooks",
    "/v1/webhooks/{hook_id}",
}


def test_openapi_paths_are_pinned(client):
    # routes.py splices router.routes into app.router.routes directly instead
    # of app.include_router(), so nothing enforces that the schema still
    # matches the documented surface. This pins it.
    assert set(client.app.openapi()["paths"].keys()) == EXPECTED_PATHS
