import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, ProviderDown, SchemaDrift
from cowbird.models import Address, Kind
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_twentytwodo import REROLLS, TwentyTwoDo

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGE_ID = "21b69c071cebbd8fd65c30bfaf652eb7"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def html(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    return {
        "/action/mailbox/create": Responses(load("create_gmail.json"), load("create_domain.json")),
        "/action/mailbox/applyToken": load("apply_token.json"),
        "/action/mailbox/message": load("messages.json"),
        "/content/": Reply(200, html("content.html")),
        "/view/": html("view.html"),
        **overrides,
    }


ADDRESS = Address("cb.fixture.one@gmail.com", "22do", state="fixture.jwt.token")


class TestTwentyTwoDoContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TwentyTwoDo(FakeTransport("22do", routes()))


async def test_it_rerolls_until_the_requested_kind_comes_back():
    domain, gmail = load("create_domain.json"), load("create_gmail.json")
    create = Responses(domain, domain, gmail)
    http = FakeTransport("22do", routes(**{"/action/mailbox/create": create}))
    address = await TwentyTwoDo(http).generate(GenerateOptions(kind=Kind.GMAIL_ALIAS))
    assert address.value == "cb.fixture.one@gmail.com"
    assert sum("/create" in url for _, url, _ in http.seen) == 3


async def test_it_gives_up_after_a_bounded_number_of_rerolls():
    http = FakeTransport("22do", routes(**{"/action/mailbox/create": load("create_domain.json")}))
    with pytest.raises(ProviderDown):
        await TwentyTwoDo(http).generate(GenerateOptions(kind=Kind.GMAIL_ALIAS))
    assert sum("/create" in url for _, url, _ in http.seen) == REROLLS


async def test_a_null_data_list_is_an_empty_inbox():
    http = FakeTransport("22do", routes(**{"/action/mailbox/message": load("messages_empty.json")}))
    assert await TwentyTwoDo(http).list(ADDRESS) == []


async def test_status_false_on_a_200_is_a_failure():
    refused = {"status": False, "msg": "Authentication required"}
    http = FakeTransport("22do", routes(**{"/action/mailbox/message": refused}))
    with pytest.raises(ProviderDown):
        await TwentyTwoDo(http).list(ADDRESS)


async def test_the_body_comes_from_the_iframe_view():
    message = await TwentyTwoDo(FakeTransport("22do", routes())).get(ADDRESS, MESSAGE_ID)
    assert "482913" in message.text
    assert message.subject == "Your verification code"


async def test_a_404_content_page_is_gone():
    http = FakeTransport("22do", routes(**{"/content/": Reply(404, "<html>404</html>")}))
    with pytest.raises(MessageGone):
        await TwentyTwoDo(http).get(ADDRESS, MESSAGE_ID)


async def test_a_content_page_without_the_iframe_is_drift():
    http = FakeTransport("22do", routes(**{"/content/": Reply(200, "<html>redesigned</html>")}))
    with pytest.raises(SchemaDrift):
        await TwentyTwoDo(http).get(ADDRESS, MESSAGE_ID)
