"""The shared replay transport every provider suite runs its contract against."""

import pytest
from cowbird.errors import ProviderDown, RateLimited, SchemaDrift
from cowbird.testing import FakeTransport, Reply, Responses


async def test_the_longest_matching_key_wins():
    http = FakeTransport("p", {"/messages": ["list"], "/messages/42": {"id": "42"}})
    assert await http.json("GET", "https://x.test/messages/42") == {"id": "42"}
    assert await http.json("GET", "https://x.test/messages") == ["list"]


async def test_responses_answer_in_order_and_the_last_one_repeats():
    http = FakeTransport("p", {"/new": Responses({"n": 1}, {"n": 2})})
    got = [(await http.json("POST", "https://x.test/new"))["n"] for _ in range(3)]
    assert got == [1, 2, 2]


async def test_an_exception_answer_is_raised():
    http = FakeTransport("p", {"/down": ProviderDown("p: HTTP 503")})
    with pytest.raises(ProviderDown):
        await http.json("GET", "https://x.test/down")


async def test_send_exposes_status_headers_and_cookies():
    http = FakeTransport(
        "p", {"/session": Reply(201, {"ok": True}, headers={"x": "1"}, cookies={"sid": "a"})}
    )
    resp = await http.send("GET", "https://x.test/session")
    assert resp.status_code == 201
    assert resp.text == '{"ok": true}'
    assert resp.headers == {"x": "1"}
    assert resp.cookies.get("sid") == "a"


async def test_send_uses_the_transport_default_status_for_a_bare_payload():
    http = FakeTransport("p", {"/x": {"a": 1}}, status=404)
    assert (await http.send("GET", "https://x.test/x")).status_code == 404


async def test_json_on_a_non_json_body_is_schema_drift():
    http = FakeTransport("p", {"/page": "<html>not json</html>"})
    with pytest.raises(SchemaDrift):
        await http.json("GET", "https://x.test/page")


async def test_text_returns_a_str_payload_unchanged():
    http = FakeTransport("p", {"/page": "<html>hi</html>"})
    assert await http.text("GET", "https://x.test/page") == "<html>hi</html>"


async def test_query_params_and_json_body_take_part_in_routing():
    # guerrillamail multiplexes one URL on ?f=, maildrop one URL on the query.
    http = FakeTransport("p", {"f=check_email": {"list": []}, "inbox(": {"data": {}}})
    assert await http.json(
        "GET", "https://x.test/ajax.php", params={"f": "check_email"}
    ) == {"list": []}
    assert await http.json(
        "POST", "https://x.test/graphql", json={"query": "{ inbox(mailbox: $m) { id } }"}
    ) == {"data": {}}


async def test_an_unrouted_request_fails_loudly():
    http = FakeTransport("p", {"/known": {}})
    with pytest.raises(AssertionError, match="unexpected request"):
        await http.json("GET", "https://x.test/unknown")


async def test_seen_records_method_url_and_kwargs():
    http = FakeTransport("p", {"/x": {}})
    await http.json("POST", "https://x.test/x", json={"a": 1}, headers={"h": "v"})
    assert http.seen == [("POST", "https://x.test/x", {"json": {"a": 1}, "headers": {"h": "v"}})]


async def test_a_reply_without_a_payload_is_an_empty_body():
    http = FakeTransport("p", {"/x": Reply(204)})
    resp = await http.send("GET", "https://x.test/x")
    assert resp.status_code == 204
    assert resp.text == ""


async def test_json_on_an_empty_body_is_schema_drift():
    http = FakeTransport("p", {"/x": Reply(200)})
    with pytest.raises(SchemaDrift):
        await http.json("GET", "https://x.test/x")


async def test_a_5xx_route_raises_provider_down_as_the_real_transport_does():
    http = FakeTransport("p", {"/x": Reply(500, {"error": "boom"})})
    with pytest.raises(ProviderDown):
        await http.send("GET", "https://x.test/x")


async def test_a_429_route_raises_rate_limited():
    http = FakeTransport("p", {"/x": Reply(429, {"error": "boom"})})
    with pytest.raises(RateLimited):
        await http.send("GET", "https://x.test/x")


async def test_json_hands_out_a_copy_of_the_fixture():
    http = FakeTransport("p", {"/x": {"rows": [1, 2]}})
    result = await http.json("GET", "https://x.test/x")
    result["rows"].append(3)
    assert await http.json("GET", "https://x.test/x") == {"rows": [1, 2]}


async def test_a_route_key_matching_a_cookie_or_header_value_routes_correctly():
    # 10minutemail replays identity in cookies, temp-mail.org/22.do in an
    # Authorization header. A route key must be able to match either, or
    # deleting the cookie from such an adapter would still pass every mocked
    # test unless cookies/headers take part in routing.
    http = FakeTransport(
        "p",
        {
            "sid=abc123": {"inbox": "cookie-routed"},
            "Bearer tok-xyz": {"inbox": "header-routed"},
        },
    )
    assert await http.json(
        "GET", "https://x.test/inbox", cookies={"sid": "abc123"}
    ) == {"inbox": "cookie-routed"}
    assert await http.json(
        "GET", "https://x.test/inbox", headers={"Authorization": "Bearer tok-xyz"}
    ) == {"inbox": "header-routed"}


async def test_response_headers_are_case_insensitive():
    http = FakeTransport("p", {"/x": Reply(200, {}, headers={"Set-Cookie": "a=b"})})
    resp = await http.send("GET", "https://x.test/x")
    assert resp.headers["set-cookie"] == "a=b"
