import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_mailticking import MailTicking

FIXTURES = Path(__file__).parent / "fixtures"
SITE = "https://www.mailticking.com"
MESSAGE_ID = "2ef8c84c020fb20c53568171695fc8b4"
CODE = (
    "e43a6fad2f505badf8f6b9eeb28bbb87ec20356cb2d496f9f07532ec"
    "8594ed90a526cef14f0fb317970adf94721049b2"
)
ADDRESS = Address("arrogancepchzxp+kish@googlemail.com", "mailticking", state=CODE)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    return {
        f"POST {SITE}/get-mailbox": Responses(load("mailbox_1.json"), load("mailbox_2.json")),
        f"POST {SITE}/activate-email": load("activate.json"),
        f"GET {SITE}/?": Responses(page("home_1.html"), page("home_2.html")),
        f"POST {SITE}/get-emails": load("emails.json"),
        f"GET {SITE}/mail/gmail-content/{MESSAGE_ID}": load("content.json"),
        **overrides,
    }


class TestMailTickingContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return MailTicking(FakeTransport("mailticking", routes()))


async def test_generate_activates_the_mailbox_and_keeps_its_code_as_state():
    http = FakeTransport("mailticking", routes())
    address = await MailTicking(http).generate()
    assert address.value == ADDRESS.value
    assert address.state == CODE
    activate = next(kw for m, url, kw in http.seen if url.endswith("/activate-email"))
    assert activate["json"]["email"] == ADDRESS.value
    assert activate["json"]["activate_token"] == load("mailbox_1.json")["activate_token"]
    home = next(kw for m, url, kw in http.seen if url == f"{SITE}/")
    assert home["cookies"] == {"active_mailbox": quote(ADDRESS.value)}


async def test_generate_asks_only_for_gmail_types():
    http = FakeTransport("mailticking", routes())
    await MailTicking(http).generate()
    _, _, kw = http.seen[0]
    assert kw["json"] == {"types": ["1", "2", "3"]}


async def test_a_page_showing_another_mailbox_is_drift():
    # If the cookie were ignored, the page's code would read someone else's inbox.
    http = FakeTransport("mailticking", routes(**{f"GET {SITE}/?": page("home_2.html")}))
    with pytest.raises(SchemaDrift):
        await MailTicking(http).generate()


async def test_a_page_without_a_code_is_drift():
    http = FakeTransport("mailticking", routes(**{f"GET {SITE}/?": "<html></html>"}))
    with pytest.raises(SchemaDrift):
        await MailTicking(http).generate()


async def test_a_mailbox_answer_without_email_or_token_is_drift():
    http = FakeTransport(
        "mailticking", routes(**{f"POST {SITE}/get-mailbox": {"success": True, "email": "x"}})
    )
    with pytest.raises(SchemaDrift):
        await MailTicking(http).generate()


async def test_a_chosen_local_or_domain_is_refused():
    provider = MailTicking(FakeTransport("mailticking", routes()))
    with pytest.raises(NotSupported):
        await provider.generate(GenerateOptions(local="hello"))
    with pytest.raises(NotSupported):
        await provider.generate(GenerateOptions(domain="gmail.com"))


async def test_list_sends_the_email_and_the_code_from_state():
    http = FakeTransport("mailticking", routes())
    rows = await MailTicking(http).list(ADDRESS)
    assert [r.id for r in rows] == [MESSAGE_ID]
    assert rows[0].sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert rows[0].subject == "JoltMx test email (ref 01a0d34b0ecc)"
    assert rows[0].received_at == datetime.fromtimestamp(1790251317, UTC)
    _, _, kw = http.seen[-1]
    assert kw["json"] == {"email": ADDRESS.value, "code": CODE}


async def test_an_empty_inbox_is_an_empty_list():
    http = FakeTransport(
        "mailticking", routes(**{f"POST {SITE}/get-emails": load("emails_empty.json")})
    )
    assert await MailTicking(http).list(ADDRESS) == []


async def test_list_without_the_code_is_refused():
    with pytest.raises(NotSupported):
        await MailTicking(FakeTransport("mailticking", routes())).list(
            Address(ADDRESS.value, "mailticking")
        )


@pytest.mark.parametrize(
    "answer",
    [
        {"success": False, "needNewEmail": True, "error": "expired"},
        {"success": True, "emails": "changed"},
        {"success": True, "emails": [{"Subject": "no code"}]},
    ],
)
async def test_a_listing_it_does_not_recognise_is_drift(answer):
    http = FakeTransport("mailticking", routes(**{f"POST {SITE}/get-emails": answer}))
    with pytest.raises(SchemaDrift):
        await MailTicking(http).list(ADDRESS)


async def test_get_reads_the_body_with_the_mailbox_cookie_and_the_headers_from_the_row():
    http = FakeTransport("mailticking", routes())
    message = await MailTicking(http).get(ADDRESS, MESSAGE_ID)
    _, url, kw = http.seen[-1]
    assert url == f"{SITE}/mail/gmail-content/{MESSAGE_ID}"
    assert kw["cookies"] == {"active_mailbox": quote(ADDRESS.value)}
    assert message.sender == "JoltMx Delivery Test <test@sendtest.joltmx.com>"
    assert message.subject == "JoltMx test email (ref 01a0d34b0ecc)"
    assert message.received_at == datetime.fromtimestamp(1790251317, UTC)
    assert "everything is working as expected" in message.text
    assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links)


async def test_a_message_the_listing_no_longer_has_is_gone():
    http = FakeTransport(
        "mailticking", routes(**{f"POST {SITE}/get-emails": load("emails_empty.json")})
    )
    with pytest.raises(MessageGone):
        await MailTicking(http).get(ADDRESS, MESSAGE_ID)


async def test_a_body_the_site_no_longer_has_is_gone():
    http = FakeTransport(
        "mailticking",
        routes(
            **{
                f"GET {SITE}/mail/gmail-content/{MESSAGE_ID}": Reply(
                    404, load("content_missing.json")
                )
            }
        ),
    )
    with pytest.raises(MessageGone):
        await MailTicking(http).get(ADDRESS, MESSAGE_ID)


async def test_a_refused_body_is_drift():
    # A 403 means the mailbox cookie no longer proves ownership: the protocol moved.
    http = FakeTransport(
        "mailticking",
        routes(
            **{
                f"GET {SITE}/mail/gmail-content/{MESSAGE_ID}": Reply(
                    403, {"error": "You don't have permission to view this email"}
                )
            }
        ),
    )
    with pytest.raises(SchemaDrift):
        await MailTicking(http).get(ADDRESS, MESSAGE_ID)
