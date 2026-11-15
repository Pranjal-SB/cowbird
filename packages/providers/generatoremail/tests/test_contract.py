import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.testing import FakeTransport
from cowbird_generatoremail import GeneratorEmail

FIXTURES = Path(__file__).parent / "fixtures"
MID = "5f17b04df7a4617dafb561fa7ea0c82d"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes(**overrides):
    return {
        "GET https://emailfake.com/api/domains.php": json.loads(load("domains.json")),
        "GET https://emailfake.com/": load("mailbox.html"),
        MID: load("message.html"),
        **overrides,
    }


class TestGeneratorEmailContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return GeneratorEmail(FakeTransport("generatoremail", routes()))
