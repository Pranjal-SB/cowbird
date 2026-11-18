import pytest
from cowbird.contract import ProviderContract
from cowbird.testing import FakeTransport, Responses
from cowbird_anonbox import Anonbox

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"

PAGE1 = (FIXTURES / "page1.html").read_text()
PAGE2 = (FIXTURES / "page2.html").read_text()
MBOX = (FIXTURES / "mbox_one.txt").read_text() if (FIXTURES / "mbox_one.txt").exists() else ""
PATH = "5at52/b3vb33iy4q"


def fake(routes):
    return FakeTransport("anonbox", routes)


def default_routes(mbox: str = MBOX):
    return {
        "/en/": Responses(PAGE1, PAGE2),
        PATH: mbox,
    }


class TestAnonboxContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return Anonbox(fake(default_routes()))


async def test_deleted_mailbox_surfaces_as_message_gone_not_schema_drift():
    # The per-minute crontab deletes fetched mailboxes; the fetch then answers
    # 404 with an HTML error page, which must become MessageGone (so callers
    # pruning expired addresses can catch it) — never SchemaDrift-from-parsing.
    from cowbird.errors import MessageGone
    from cowbird.testing import Reply

    http = fake({**default_routes(), PATH: Reply(status=404, payload="<html>not found</html>")})
    provider = Anonbox(http)
    address = await provider.generate()
    with pytest.raises(MessageGone):
        await provider.get(address, "<x@example.test>")
    with pytest.raises(MessageGone):
        await provider.list(address)



@pytest.mark.parametrize("state", ["not json", "{}", "[]"])
async def test_malformed_state_is_not_supported(state):
    from cowbird.errors import NotSupported
    from cowbird.models import Address
    from cowbird_anonbox import Anonbox

    with pytest.raises(NotSupported):
        await Anonbox(fake(default_routes())).list(Address("x@anonbox.net", "anonbox", state=state))
