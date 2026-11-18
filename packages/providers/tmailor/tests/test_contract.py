import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Responses
from cowbird_tmailor import Tmailor

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes, status=200):
    return FakeTransport("tmailor", routes, status)


def default_routes():
    # Responses instances are built per call: a module-level one would share its
    # call counter across every test (see cowbird.testing.Responses).
    return {
        "webapp-newemail": Responses(load("newemail.json"), load("newemail_rotate.json")),
        # listinbox always answers with the full page: the contract's
        # empty-inbox test only checks the return type, while its get test
        # must see a row or it skips the body-read path — the exact path
        # where the Message.id/delete mismatch lived.
        "listToken": load("listinbox_full.json"),
        "webapp-read": load("read.json"),
        "webapp-delete": load("ok.json"),
        "GET https://tmailor.com/": (FIXTURES / "home.html").read_text(),
    }


def _state(address):
    return json.loads(address.state)


class TestTmailorContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return Tmailor(fake(default_routes()))


async def test_generate_posts_the_newemail_body_and_packs_state():
    http = fake(default_routes())
    address = await Tmailor(http).generate(GenerateOptions())
    method, url, kw = http.seen[0]
    assert (method, url) == ("POST", "https://tmailor.com/api/webapp-newemail")
    assert kw["json"]["action"] == "newemail"
    assert kw["json"]["choose_domain"] == 1
    assert address.provider == "tmailor"
    assert "@" in address.value
    # The per-mailbox JWT is the only credential; it must travel in state.
    packed = _state(address)
    assert packed["token"] and packed["address"] == address.value


async def test_generate_missing_accesstoken_is_schema_drift():
    broken = load("newemail.json")
    del broken["accesstoken"]
    http = fake({**default_routes(), "webapp-newemail": broken})
    with pytest.raises(SchemaDrift, match="accesstoken"):
        await Tmailor(http).generate(GenerateOptions())


@pytest.mark.parametrize("block", ["errorcaptcha", "client-block", "cfblock"])
async def test_a_blocked_egress_is_down_not_drift(block):
    # These answers mean this IP is blocked for now (recon saw one last about
    # ten minutes); the adapter is fine, so it must not quarantine.
    from cowbird.errors import ProviderDown

    http = fake({**default_routes(), "webapp-newemail": {"msg": block}})
    with pytest.raises(ProviderDown, match=block):
        await Tmailor(http).generate(GenerateOptions())


async def test_an_unknown_msg_is_still_drift():
    http = fake({**default_routes(), "webapp-newemail": {"msg": "somethingnew"}})
    with pytest.raises(SchemaDrift, match="somethingnew"):
        await Tmailor(http).generate(GenerateOptions())


async def test_list_uses_the_listtoken_map_shape():
    # The single-token variant ({"curentToken": jwt}) returns errortoken; only
    # the {email: token} map works. The adapter must send the map, always.
    http = fake(default_routes())
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    rows = await provider.list(address)
    method, url, kw = http.seen[-1]
    assert url.endswith("/webapp-listinbox")
    assert kw["json"]["listToken"] == {address.value: _state(address)["token"]}
    assert len(rows) == 1


async def test_list_parses_rows_from_the_data_map():
    http = fake(default_routes())
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    rows = await provider.list(address)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == "YU1lc3NhZ2VJZA=="  # the email_id blob: get() reads by it
    assert row.sender == "noreply@example.test"
    assert row.subject == "Your verification code is 482913."
    assert row.received_at is not None


async def test_list_errortoken_is_address_expired():
    # What the server answers when the mailbox token is rejected: the
    # mailbox's credential is dead, not the adapter.
    from cowbird.errors import AddressExpired

    http = fake({**default_routes(), "listToken": {"msg": "errortoken"}})
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    with pytest.raises(AddressExpired, match="errortoken"):
        await provider.list(address)


async def test_row_missing_email_id_is_schema_drift():
    broken = load("listinbox_full.json")
    box = broken["data"]["probe482913@topdatamaster.com"]["data"]
    del next(iter(box.values()))["email_id"]
    http = fake({**default_routes(), "listToken": broken})
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    with pytest.raises(SchemaDrift, match="email_id"):
        await provider.list(address)


async def test_get_reads_with_email_code_and_email_id():
    http = fake(default_routes())
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    message = await provider.get(address, "YU1lc3NhZ2VJZA==")
    method, url, kw = http.seen[-1]
    assert url.endswith("/webapp-read")
    assert kw["json"]["email_code"] == "22222222-3333-4444-8555-666666666666"
    assert kw["json"]["email_token"] == "YU1lc3NhZ2VJZA=="
    assert kw["json"]["accesstoken"] == _state(address)["token"]
    assert message.text == "Your verification code is 482913."
    assert message.sender == "noreply@example.test"


async def test_read_errortoken_is_schema_drift():
    http = fake({**default_routes(), "webapp-read": {"msg": "error", "code": "errortoken"}})
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    with pytest.raises(SchemaDrift):
        await provider.get(address, "YU1lc3NhZ2VJZA==")


async def test_a_message_no_longer_listed_is_gone_not_drift():
    # Expired or deleted between list and get: webapp-read would answer
    # errortoken (SchemaDrift) and kill a watch() stream; the fresh list page
    # is checked first so the caller gets MessageGone instead.
    http = fake({**default_routes(), "listToken": load("listinbox_empty.json")})
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    with pytest.raises(MessageGone):
        await provider.get(address, "YU1lc3NhZ2VJZA==")


async def test_a_non_numeric_domain_option_value_is_drift():
    # The select's values are numeric backend flags; anything else means the
    # scrape matched something that is not the choose_domain select.
    weird_home = (
        '<select id="domains"><option value="abc">tinpho.com</option></select>'
    )
    http = fake({**default_routes(), "GET https://tmailor.com/": weird_home})
    with pytest.raises(SchemaDrift):
        await Tmailor(http).generate(GenerateOptions(domain="tinpho.com"))


async def test_delete_sends_email_id_and_code():
    http = fake(default_routes())
    provider = Tmailor(http)
    address = await provider.generate(GenerateOptions())
    await provider.delete(address, "YU1lc3NhZ2VJZA==")
    method, url, kw = http.seen[-1]
    assert url.endswith("/webapp-delete")
    assert kw["json"]["email_token"] == "YU1lc3NhZ2VJZA=="
    assert kw["json"]["email_code"]


async def test_a_custom_local_part_is_not_supported():
    # The server issues random local parts; the site's custom-name flow is a
    # captcha-gated UI path we do not implement.
    http = fake(default_routes())
    with pytest.raises(NotSupported):
        await Tmailor(http).generate(GenerateOptions(local="choosy"))


async def test_a_domain_outside_the_scraped_options_is_refused():
    # The homepage select is the only place domains ship to the client; it
    # offers only "Random domain", so any named domain is a caller error.
    http = fake(default_routes())
    with pytest.raises(NotSupported):
        await Tmailor(http).generate(GenerateOptions(domain="tinpho.com"))
