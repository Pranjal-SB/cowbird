from __future__ import annotations


def test_a_missing_key_is_rejected(client):
    response = client.get("/v1/providers")
    assert response.status_code == 401
    assert response.json() == {
        "success": False,
        "data": None,
        "error": "invalid or missing API key",
    }


def test_a_wrong_key_is_rejected(client, auth):
    response = client.get("/v1/providers", headers={"x-api-key": auth["x-api-key"] + "x"})
    assert response.status_code == 401


def test_a_valid_key_reaches_the_route(client, auth):
    response = client.get("/v1/providers", headers=auth)
    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "provider": "fake",
            "status": "ok",
            "p50": None,
            "kind": ["own-domain"],
            "sites": ["fake.test"],
        }
    ]


def test_the_provider_matrix_never_leaks_address_state(client, auth):
    # Blunt guard. `state` is a provider credential and must not appear in any
    # response body; this is the cheapest place to pin that habit.
    assert "state" not in client.get("/v1/providers", headers=auth).text
