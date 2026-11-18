from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.testing import FakeTransport, Reply
from cowbird_mailnesia import Mailnesia

FIXTURES = Path(__file__).parent / "fixtures"

# Mirrors test_mailnesia's route table: the bare-id keys catch the message
# page and its 404 whatever local part generate() coined, and the /mailbox/
# fallback catches the inbox listing for random locals.
DEFAULT_ROUTES = {
    "/mailbox/cbprobe?": (FIXTURES / "mailbox.html").read_text(),
    "1299827515": (FIXTURES / "message.html").read_text(),
    "9999999999": Reply(404, ""),
    "/mailbox/": (FIXTURES / "mailbox.html").read_text(),
}


class TestMailnesiaContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return Mailnesia(FakeTransport("mailnesia", dict(DEFAULT_ROUTES)))
