import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift, SolverUnavailable
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeSolver, FakeTransport, Reply, Responses
from cowbird_vanishinbox import EDU_DOMAINS, PAGE, SITEKEY, VanishInbox

FIXTURES = Path(__file__).parent / "fixtures"
ADDR = "swiftwren234@fommie.com"
MID = "babe3949-61f1-4e8d-ac37-eee80a990dbb"
# vi_hv is "<unix expiry>.<hmac>"; the hmac here is a placeholder.
LIVE_PASS = f"{int(time.time()) + 3600}.COOKIE-PLACEHOLDER"
OLD_PASS = f"{int(time.time()) - 60}.OLD-COOKIE-PLACEHOLDER"

VERIFY = "POST https://vanishinbox.com/api/verify-turnstile"
INBOX = "GET https://vanishinbox.com/api/inbox/"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def verified(cookie=LIVE_PASS):
    return Reply(200, load("verify_ok.json"), cookies={"vi_hv": cookie})


def routes(**overrides):
    return {VERIFY: verified(), INBOX: load("inbox.json"), **overrides}


def vanish(solver=None, **overrides):
    http = FakeTransport("vanishinbox", routes(**overrides), solver=solver or FakeSolver())
    return VanishInbox(http), http


def calls(http, prefix):
    method, _, url = prefix.partition(" ")
    return [kw for m, u, kw in http.seen if m == method and u.startswith(url)]


def state(cookie=LIVE_PASS):
    return json.dumps({"vi_hv": cookie}, separators=(",", ":"))


ADDRESS = Address(ADDR, "vanishinbox", state=state())


class TestVanishInboxContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return vanish()[0]


async def test_generate_posts_one_solved_token_to_verify():
    provider, http = vanish()
    await provider.generate()
    assert provider.http.solver.calls == [("turnstile", PAGE, SITEKEY)]
    assert [kw["json"] for kw in calls(http, VERIFY)] == [{"token": "fake-token"}]


async def test_generate_carries_the_pass_cookie_in_state():
    address = await vanish()[0].generate()
    assert json.loads(address.state) == {"vi_hv": LIVE_PASS}


async def test_one_pass_serves_every_address_until_it_expires():
    provider, http = vanish()
    first = await provider.generate()
    second = await provider.generate()
    await provider.list(first)
    await provider.list(second)
    assert len(provider.http.solver.calls) == 1
    assert len(calls(http, VERIFY)) == 1


async def test_list_sends_the_pass_as_a_cookie_on_an_escaped_address():
    provider, http = vanish()
    await provider.list(ADDRESS)
    [kw] = calls(http, INBOX)
    assert kw["cookies"] == {"vi_hv": LIVE_PASS}
    assert ("GET", "https://vanishinbox.com/api/inbox/swiftwren234%40fommie.com") in {
        (m, u) for m, u, _ in http.seen
    }


async def test_list_from_state_alone_needs_no_solve():
    provider, _ = vanish()
    assert [r.id for r in await provider.list(ADDRESS)] == [MID]
    assert provider.http.solver.calls == []


async def test_list_without_state_solves_once():
    provider, http = vanish()
    await provider.list(Address(ADDR, "vanishinbox"))
    await provider.list(Address(ADDR, "vanishinbox"))
    assert len(provider.http.solver.calls) == 1
    assert all(kw["cookies"] == {"vi_hv": LIVE_PASS} for kw in calls(http, INBOX))


async def test_an_expired_pass_in_state_is_not_sent():
    provider, http = vanish()
    await provider.list(Address(ADDR, "vanishinbox", state=state(OLD_PASS)))
    assert len(provider.http.solver.calls) == 1
    assert calls(http, INBOX)[0]["cookies"] == {"vi_hv": LIVE_PASS}


async def test_a_revoked_pass_is_renewed_once_and_the_read_retried():
    provider, http = vanish(
        **{INBOX: Responses(Reply(403, load("inbox_unverified.json")), load("inbox.json"))}
    )
    rows = await provider.list(ADDRESS)
    assert [r.id for r in rows] == [MID]
    assert len(provider.http.solver.calls) == 1
    assert len(calls(http, INBOX)) == 2


async def test_a_pass_refused_right_after_verifying_is_drift():
    provider, _ = vanish(**{INBOX: Reply(403, load("inbox_unverified.json"))})
    with pytest.raises(SchemaDrift):
        await provider.list(ADDRESS)
    assert len(provider.http.solver.calls) == 1


async def test_list_reads_the_rows():
    [row] = await vanish()[0].list(ADDRESS)
    assert row.id == MID
    assert row.sender == "test@sendtest.joltmx.com"
    assert row.subject == "JoltMx test email (ref 01a0d68d9059)"
    assert row.received_at == datetime(2026, 9, 25, 3, 13, 17, 944000, tzinfo=UTC)


async def test_get_reads_the_body_from_the_listing():
    message = await vanish()[0].get(ADDRESS, MID)
    assert message.id == MID
    assert message.html.startswith("<!DOCTYPE html>")
    assert message.text.startswith("This is a test email from JoltMx.")
    assert "https://joltmx.com" in message.links


async def test_an_empty_inbox_is_an_empty_list():
    provider, _ = vanish(**{INBOX: load("inbox_empty.json")})
    assert await provider.list(ADDRESS) == []


async def test_a_message_no_longer_listed_is_gone():
    # There is no per-message endpoint: the listing carries the bodies, and a
    # message the site has dropped is simply absent from it.
    provider, _ = vanish(**{INBOX: load("inbox_empty.json")})
    with pytest.raises(MessageGone):
        await provider.get(ADDRESS, MID)


async def test_without_a_solver_generate_is_not_supported():
    provider = VanishInbox(FakeTransport("vanishinbox", routes()))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.generate()


async def test_without_a_solver_list_without_a_pass_is_not_supported():
    provider = VanishInbox(FakeTransport("vanishinbox", routes()))
    with pytest.raises(NotSupported, match="COWBIRD_SOLVER_URL"):
        await provider.list(Address(ADDR, "vanishinbox"))


async def test_a_token_the_site_refuses_is_the_solvers_fault_not_drift():
    provider, _ = vanish(**{VERIFY: Reply(403, load("verify_refused.json"))})
    with pytest.raises(SolverUnavailable):
        await provider.generate()


async def test_solver_unavailable_propagates_unchanged():
    failure = SolverUnavailable("solver /turnstile: HTTP 500")
    provider, http = vanish(FakeSolver(token=failure))
    with pytest.raises(SolverUnavailable) as caught:
        await provider.generate()
    assert caught.value is failure
    assert not calls(http, VERIFY)


@pytest.mark.parametrize(
    "route, answer",
    [
        (VERIFY, Reply(200, load("verify_ok.json"))),  # success but no cookie
        (VERIFY, Reply(200, {"x": 1}, cookies={"vi_hv": LIVE_PASS})),
        (INBOX, {"x": 1}),
        (INBOX, {"emails": [{"subject": "no id"}]}),
        (INBOX, {"emails": [{"id": MID, "body": None}]}),
    ],
)
async def test_a_malformed_answer_is_drift(route, answer):
    provider, _ = vanish(**{route: answer})
    with pytest.raises(SchemaDrift):
        await provider.get(Address(ADDR, "vanishinbox"), MID)


async def test_the_edu_kind_gives_an_edu_pl_address():
    address = await vanish()[0].generate(GenerateOptions(kind=Kind.EDU))
    assert address.value.rpartition("@")[2] in EDU_DOMAINS


async def test_a_chosen_local_part_and_domain_are_kept():
    address = await vanish()[0].generate(GenerateOptions(local="me.test", domain="remind.edu.pl"))
    assert address.value == "me.test@remind.edu.pl"


@pytest.mark.parametrize(
    "opts",
    [
        GenerateOptions(domain="gmail.com"),
        GenerateOptions(kind=Kind.GMAIL_ALIAS),
        GenerateOptions(kind=Kind.EDU, domain="fommie.com"),
        GenerateOptions(local="bad/local"),
    ],
)
async def test_what_the_site_cannot_do_is_refused_before_a_solve(opts):
    provider, _ = vanish()
    with pytest.raises(NotSupported):
        await provider.generate(opts)
    assert provider.http.solver.calls == []
