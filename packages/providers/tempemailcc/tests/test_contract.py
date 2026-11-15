import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.testing import FakeTransport
from cowbird_tempemailcc import TempEmailCc

FIXTURES = Path(__file__).parent / "fixtures"
MID = "247827277901992001"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    return {
        "POST https://www.tempemail.cc/api/accounts": json.loads(load("create.json")),
        "GET https://www.tempemail.cc/api/messages": json.loads(load("messages_one.json")),
        "GET https://www.tempemail.cc/api/me": json.loads(load("me.json")),
        "GET https://www.tempemail.cc/api/messages/" + MID: json.loads(load("message_detail.json")),
        **overrides,
    }


class TestTempEmailCcContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TempEmailCc(FakeTransport("tempemailcc", routes()))
