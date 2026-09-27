import socket

import pytest
from cowbird.errors import SolverUnavailable
from cowbird.solver import Clearance, Solver

IUAM_ANSWER = {
    "headers": {
        "Cookie": "cf_clearance=abc.def-123; __cf_bm=xyz",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/140.0.0.0",
    },
    "ip": "203.0.113.7",
    "elapsed": 6.1,
}


def solver_answering(answer, **kw) -> tuple[Solver, list]:
    """A Solver whose HTTP call is replaced; records (path, body)."""
    sent: list = []
    solver = Solver("http://solver.test:407", **kw)

    async def post(path, body):
        sent.append((path, body))
        if isinstance(answer, Exception):
            raise answer
        return answer

    solver._post = post
    return solver, sent


async def test_clearance_parses_the_cookie_header_and_user_agent():
    solver, sent = solver_answering(IUAM_ANSWER)
    clearance = await solver.clearance("https://10minutemail.com/")
    assert clearance == Clearance(
        cookies={"cf_clearance": "abc.def-123", "__cf_bm": "xyz"},
        user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/140.0.0.0",
    )
    assert sent == [("/iuam", {"url": "https://10minutemail.com/"})]


async def test_clearance_without_cf_clearance_is_unavailable_not_drift():
    answer = {"headers": {"Cookie": "__cf_bm=xyz", "User-Agent": "UA"}}
    solver, _ = solver_answering(answer)
    with pytest.raises(SolverUnavailable):
        await solver.clearance("https://x.test/")


async def test_clearance_without_a_user_agent_is_unavailable():
    answer = {"headers": {"Cookie": "cf_clearance=a"}}
    solver, _ = solver_answering(answer)
    with pytest.raises(SolverUnavailable):
        await solver.clearance("https://x.test/")


async def test_turnstile_returns_the_token_and_sends_optional_fields_only_when_set():
    solver, sent = solver_answering({"token": "0.tok", "elapsed": 3, "status": "success"})
    assert await solver.turnstile("https://smailpro.com/", "0xKEY") == "0.tok"
    assert await solver.turnstile("https://smailpro.com/", "0xKEY", action="login") == "0.tok"
    assert sent[0] == ("/turnstile", {"url": "https://smailpro.com/", "sitekey": "0xKEY"})
    assert sent[1][1]["action"] == "login"
    assert "cdata" not in sent[1][1]


@pytest.mark.parametrize("answer", [{"token": ""}, {"status": "failed"}, "not an object"])
async def test_turnstile_without_a_token_is_unavailable(answer):
    solver, _ = solver_answering(answer)
    with pytest.raises(SolverUnavailable):
        await solver.turnstile("https://x.test/", "0xKEY")


async def test_the_proxy_is_forwarded_only_when_set():
    solver, sent = solver_answering(IUAM_ANSWER, proxy="http://user:pw@proxy.test:8080")
    await solver.clearance("https://x.test/")
    assert sent[0][1]["proxy"] == "http://user:pw@proxy.test:8080"


def test_with_proxy_returns_a_new_solver_and_leaves_the_original_alone():
    original = Solver("http://solver.test:407")
    proxied = original.with_proxy("http://proxy.test:8080")
    assert proxied.proxy == "http://proxy.test:8080"
    assert original.proxy is None
    assert proxied.url == original.url


def test_the_clearance_cookie_is_not_in_its_repr():
    clearance = Clearance(cookies={"cf_clearance": "secret"}, user_agent="UA")
    assert "secret" not in repr(clearance)


async def test_a_solver_nobody_is_listening_on_is_unavailable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    solver = Solver(f"http://127.0.0.1:{port}", timeout=5)
    with pytest.raises(SolverUnavailable):
        await solver.clearance("https://x.test/")


def test_from_env_is_none_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("COWBIRD_SOLVER_URL", raising=False)
    assert Solver.from_env() is None
    monkeypatch.setenv("COWBIRD_SOLVER_URL", "  ")
    assert Solver.from_env() is None


def test_from_env_builds_a_solver(monkeypatch):
    monkeypatch.setenv("COWBIRD_SOLVER_URL", "http://localhost:407/")
    solver = Solver.from_env()
    assert solver is not None and solver.url == "http://localhost:407"


def test_from_env_rejects_a_value_that_is_not_a_url(monkeypatch):
    # A typo must fail the process, not quietly run without a solver.
    monkeypatch.setenv("COWBIRD_SOLVER_URL", "localhost:407")
    with pytest.raises(ValueError):
        Solver.from_env()


def test_solver_unavailable_reroutes_but_is_not_a_provider_error():
    from cowbird.errors import ProviderError

    assert SolverUnavailable.reroutable is True
    assert not issubclass(SolverUnavailable, ProviderError)
