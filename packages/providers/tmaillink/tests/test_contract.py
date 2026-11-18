from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_tmaillink import TMailLink

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _home(addr: str) -> Reply:
    html = load("home.html").replace("francis.lum", addr)
    return Reply(200, html, cookies={"csrftoken": "contract-token"})


def routes(**overrides):
    return {
        "GET https://tmail.link/": Responses(
            _home("francis.lum"), _home("marie.hahn"), _home("francis.lum")
        ),
        "GET https://tmail.link/inbox/francis.lum@tmail.link/": Reply(
            200, load("home.html"), cookies={"csrftoken": "contract-token"}
        ),
        "GET https://tmail.link/inbox/marie.hahn@tmail.link/": Reply(
            200, load("home.html"), cookies={"csrftoken": "contract-token"}
        ),
        "GET https://tmail.link/inbox/reader@tmail.link/": Reply(
            200, load("home.html"), cookies={"csrftoken": "contract-token"}
        ),
        "POST https://tmail.link/inbox/francis.lum@tmail.link/": load("list_one.json"),
        "POST https://tmail.link/inbox/reader@tmail.link/": load("list_one.json"),
        KEY: load("message.html"),
        f"{KEY}/1": load("message_body.html"),
        **overrides,
    }


class TestTMailLinkContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return TMailLink(FakeTransport("tmaillink", routes()))
