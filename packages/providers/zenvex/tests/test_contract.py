import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift, SolverUnavailable
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeSolver, FakeTransport, Reply, Responses
from cowbird_zenvex import PAGE, SITEKEY, Zenvex

FIXTURES = Path(__file__).parent / "fixtures"
CSRF = "CSRF-PLACEHOLDER"

HOME = "GET https://zenvex.dev/?"
DOMAINS = "GET https://zenvex.dev/api/domains"
VERIFY = "POST https://zenvex.dev/turnstile/verify"
EMAILS = "GET https://zenvex.dev/api/emails/"
INBOX = "GET https://zenvex.dev/api/inbox/"
MID = "h7439esj8qgjdgwj2t5avopt"
ADDRESS = Address("cbb15c6d7c@souss.dev", "zenvex")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


UNVERIFIED = Reply(403, load("unverified.json"))


def routes(**overrides):
    return {
        HOME: Reply(
            200, "<!doctype html>", cookies={"zvx_sid": "SID-PLACEHOLDER", "zvx_csrf": CSRF}
        ),
        DOMAINS: load("domains.json"),
        VERIFY: load("verify_ok.json"),
        EMAILS: load("emails.json"),
        INBOX: load("message.json"),
        **overrides,
    }


def zenvex(solver=None, **overrides):
    http = FakeTransport("zenvex", routes(**overrides), solver=solver or FakeSolver())
    return Zenvex(http), http


def headers_of(http, prefix):
    method, _, url = prefix.partition(" ")
    return [kw.get("headers") or {} for m, u, kw in http.seen if m == method and u.startswith(url)]


class TestZenvexContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return zenvex()[0]


async def test_the_first_read_passes_turnstile_and_sends_the_token_to_verify():
    provider, http = zenvex(**{EMAILS: Responses(UNVERIFIED, load("emails.json"))})
    rows = await provider.list(ADDRESS)
    assert [r.id for r in rows] == [MID]
    assert provider.http.solver.calls == [("turnstile", PAGE, SITEKEY)]
    _, _, kw = next(s for s in http.seen if s[1].endswith("/turnstile/verify"))
    assert kw["json"] == {"token": "fake-token"}
    assert kw["headers"]["X-Zenvex-CSRF"] == CSRF


async def test_one_solve_serves_every_address_on_the_transport():
    provider, _ = zenvex(**{EMAILS: Responses(UNVERIFIED, load("emails_empty.json"))})
    await provider.list(ADDRESS)
    await provider.list(Address("other01@znvx.me", "zenvex"))
    await provider.get(ADDRESS, MID)
    assert len(provider.http.solver.calls) == 1


class SlowSolver(FakeSolver):
    async def turnstile(self, url, sitekey, **kw):
        await asyncio.sleep(0.01)
        return await super().turnstile(url, sitekey, **kw)


async def test_concurrent_reads_on_an_unverified_session_share_one_solve():
    empty = load("emails_empty.json")
    provider, _ = zenvex(SlowSolver(), **{EMAILS: Responses(UNVERIFIED, UNVERIFIED, empty)})
    await asyncio.gather(provider.list(ADDRESS), provider.list(ADDRESS))
    assert len(provider.http.solver.calls) == 1


async def test_a_lapsed_verification_is_renewed_once_with_a_fresh_home_visit():
    emails = load("emails.json")
    provider, http = zenvex(**{EMAILS: Responses(UNVERIFIED, emails, UNVERIFIED, emails)})
    await provider.list(ADDRESS)
    assert await provider.list(ADDRESS)
    assert len(provider.http.solver.calls) == 2
    assert sum(1 for m, u, _ in http.seen if u == PAGE) == 2


async def test_a_session_refused_right_after_verifying_is_drift():
    provider, _ = zenvex(**{EMAILS: UNVERIFIED})
    with pytest.raises(SchemaDrift):
        await provider.list(ADDRESS)
    assert len(provider.http.solver.calls) == 1


async def test_list_reads_the_rows():
    rows = await zenvex()[0].list(ADDRESS)
    assert rows[0].sender == "test@sendtest.joltmx.com"
    assert rows[0].subject == "JoltMx test email (ref 01a0d65c6e06)"
    assert rows[0].received_at == datetime.fromtimestamp(1790302778, UTC)


async def test_list_asks_for_the_address_it_was_given():
    provider, http = zenvex()
    await provider.list(ADDRESS)
    _, url, kw = next(s for s in http.seen if "/api/emails/" in s[1])
    assert url == "https://zenvex.dev/api/emails/cbb15c6d7c%40souss.dev"
    assert kw["params"] == {"limit": "50", "offset": "0"}


async def test_an_empty_inbox_is_an_empty_list():
    provider, _ = zenvex(**{EMAILS: load("emails_empty.json")})
    assert await provider.list(ADDRESS) == []


async def test_get_reads_the_body():
    message = await zenvex()[0].get(ADDRESS, MID)
    assert message.id == MID
    assert message.sender == "test@sendtest.joltmx.com"
    assert message.subject == "JoltMx test email (ref 01a0d65c6e06)"
    assert message.received_at == datetime.fromtimestamp(1790302778, UTC)
    assert message.html.startswith("<!DOCTYPE html>")
    assert message.text.startswith("This is a test email from JoltMx.")


async def test_a_message_zenvex_no_longer_has_is_gone():
    provider, _ = zenvex(**{INBOX: Reply(404, load("message_gone.json"))})
    with pytest.raises(MessageGone):
        await provider.get(ADDRESS, MID)


@pytest.mark.parametrize(
    "route, answer",
    [
        (EMAILS, {"success": True, "result": {"x": 1}}),
        (EMAILS, {"success": True, "result": [{"subject": "no id"}]}),
        (EMAILS, "<html>not json</html>"),
        (INBOX, {"success": True, "result": {"id": MID}}),
        (INBOX, Reply(400, {"success": False, "error": {"name": "ValidationError"}})),
    ],
)
async def test_a_malformed_answer_is_drift(route, answer):
    provider, _ = zenvex(**{route: answer})
    with pytest.raises(SchemaDrift):
        await provider.get(ADDRESS, MID) if route == INBOX else await provider.list(ADDRESS)


async def test_generate_uses_the_sites_default_domain_and_costs_no_solve():
    provider, http = zenvex()
    address = await provider.generate()
    local, _, domain = address.value.partition("@")
    assert domain == "souss.dev"
    assert len(local) >= 8 and local.isalnum()
    assert provider.http.solver.calls == []


async def test_api_calls_carry_the_csrf_cookie_as_a_header_and_look_like_fetch():
    provider, http = zenvex()
    await provider.generate()
    sent = headers_of(http, DOMAINS)[0]
    assert sent["X-Zenvex-CSRF"] == CSRF
    assert sent["Sec-Fetch-Site"] == "same-origin"
    assert sent["Sec-Fetch-Mode"] == "cors"


async def test_the_home_page_is_fetched_once_per_transport():
    provider, http = zenvex()
    await provider.generate()
    await provider.generate()
    assert sum(1 for m, u, _ in http.seen if u == "https://zenvex.dev/") == 1


async def test_a_chosen_local_and_domain_are_kept():
    address = await zenvex()[0].generate(GenerateOptions(local="qa.bot-1", domain="znvx.me"))
    assert address.value == "qa.bot-1@znvx.me"


async def test_the_edu_kind_picks_an_edu_pl_domain():
    address = await zenvex()[0].generate(GenerateOptions(kind=Kind.EDU))
    assert address.value.endswith(".edu.pl")


@pytest.mark.parametrize(
    "opts",
    [
        GenerateOptions(domain="gmail.com"),
        GenerateOptions(kind=Kind.GMAIL_ALIAS),
        GenerateOptions(kind=Kind.EDU, domain="souss.dev"),
        GenerateOptions(local="Not Allowed!"),
    ],
)
async def test_what_zenvex_cannot_serve_is_refused(opts):
    with pytest.raises(NotSupported):
        await zenvex()[0].generate(opts)


async def test_without_a_solver_generate_is_not_supported():
    provider = Zenvex(FakeTransport("zenvex", routes()))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.generate()


async def test_a_home_page_without_a_csrf_cookie_is_drift():
    provider, _ = zenvex(**{HOME: Reply(200, "<!doctype html>")})
    with pytest.raises(SchemaDrift):
        await provider.generate()


async def test_malformed_domains_are_drift():
    provider, _ = zenvex(**{DOMAINS: {"success": True, "result": {"x": 1}}})
    with pytest.raises(SchemaDrift):
        await provider.generate()


async def test_without_a_solver_list_is_not_supported():
    provider = Zenvex(FakeTransport("zenvex", routes(**{EMAILS: UNVERIFIED})))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.list(ADDRESS)


async def test_a_token_zenvex_refuses_is_the_solvers_fault_not_drift():
    provider, _ = zenvex(**{EMAILS: UNVERIFIED, VERIFY: Reply(403, load("verify_refused.json"))})
    with pytest.raises(SolverUnavailable):
        await provider.list(ADDRESS)


async def test_solver_unavailable_propagates_unchanged():
    failure = SolverUnavailable("solver /turnstile: HTTP 500")
    provider, http = zenvex(FakeSolver(token=failure), **{EMAILS: UNVERIFIED})
    with pytest.raises(SolverUnavailable) as caught:
        await provider.list(ADDRESS)
    assert caught.value is failure
    assert not headers_of(http, VERIFY)
