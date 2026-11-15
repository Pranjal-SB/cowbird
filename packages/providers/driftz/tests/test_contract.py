import json
from pathlib import Path

import pytest
from cowbird.contract import ProviderContract
from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, InboxPrivacy
from cowbird.provider import GenerateOptions
from cowbird.testing import FakeTransport, Reply, Responses
from cowbird_driftz import Driftz

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def fake(routes, status=200):
    return FakeTransport("driftz", routes, status)


DEFAULT_ROUTES = {
    "/domains": load("domains.json"),
    "/temp/generate": load("generate.json"),
    "/temp/u3thvr9w0jbixaa0x185quyh@manornewtech.org?limit=100": load("temp_list_empty.json"),
    "/emails/test@driftz.net?limit=100": load("emails_empty.json"),
    "/emails/probe@driftz.net?limit=100": load("emails_empty.json"),
}


class TestDriftzContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        # Two recorded generate() responses: the backend mints a fresh random
        # address per call, so the contract's two-generates test must see both.
        routes = dict(DEFAULT_ROUTES)
        routes["/temp/generate"] = Responses(load("generate.json"), load("generate2.json"))
        return Driftz(fake(routes))


async def test_generate_hits_temp_generate_and_carries_the_ttl():
    http = fake(dict(DEFAULT_ROUTES))
    address = await Driftz(http).generate(GenerateOptions())
    assert [m for m, *_ in http.seen] == ["POST"]
    assert address.provider == "driftz"
    assert address.value.endswith("@manornewtech.org")
    assert address.expires_at is not None


async def test_custom_local_on_a_public_domain_needs_no_http():
    # list-any-address: a permanent inbox is just an address; generate() must
    # not waste a POST /temp/generate (which would mint a different address).
    http = fake(dict(DEFAULT_ROUTES))
    address = await Driftz(http).generate(GenerateOptions(local="probe", domain="driftz.net"))
    assert http.seen == []
    assert address.value == "probe@driftz.net"
    assert await Driftz(http).list(address) == []


async def test_a_domain_outside_the_declared_set_is_refused():
    http = fake(dict(DEFAULT_ROUTES))
    with pytest.raises(NotSupported):
        await Driftz(http).generate(GenerateOptions(local="x", domain="not-a-domain.test"))


async def test_a_domain_only_request_gets_a_random_local_not_none():
    # GenerateOptions allows domain without local; the address must never
    # leak a Python None into the local part.
    http = fake(dict(DEFAULT_ROUTES))
    address = await Driftz(http).generate(GenerateOptions(domain="driftz.net"))
    assert address.value.startswith("cb")
    assert address.value.endswith("@driftz.net")
    assert "None" not in address.value


async def test_a_custom_local_on_a_temp_domain_is_refused():
    # Temp inboxes exist only via POST /temp/generate; a "custom" address on a
    # temp domain could never receive mail and would only quarantine later.
    http = fake(dict(DEFAULT_ROUTES))
    with pytest.raises(NotSupported):
        await Driftz(http).generate(GenerateOptions(local="x", domain="bbjbinin.mn"))


async def test_a_non_dict_row_is_schema_drift_not_silently_dropped():
    http = fake(
        {
            **DEFAULT_ROUTES,
            "/emails/test@driftz.net?limit=100": {
                "success": True,
                "result": {
                    "items": ["garbage"],
                    "nextCursor": None,
                    "locked": False,
                    "isPrivate": False,
                },
            },
        }
    )
    with pytest.raises(SchemaDrift, match="dict"):
        await Driftz(http).list(Address("test@driftz.net", "driftz"))


async def test_a_missing_envelope_is_schema_drift():
    # Every driftz response is {"success": true, "result": ...}. An HTML error
    # page, a bare array, or success:false means the shape moved.
    http = fake({**DEFAULT_ROUTES, "/temp/generate": {"success": False, "error": "boom"}})
    with pytest.raises(SchemaDrift):
        await Driftz(http).generate(GenerateOptions())


async def test_a_missing_items_field_is_schema_drift_naming_items():
    http = fake(
        {**DEFAULT_ROUTES, "/emails/test@driftz.net?limit=100": {"success": True, "result": {}}}
    )
    with pytest.raises(SchemaDrift, match="items"):
        await Driftz(http).list(Address("test@driftz.net", "driftz"))


async def test_a_row_missing_id_raises_schema_drift_naming_id():
    row = {
        "fromAddress": "noreply@example.test",
        "subject": "Your verification code is 482913.",
        "receivedAt": 1794740000,
        "expiresAt": 1794743600,
        "hasAttachments": False,
        "attachmentCount": 0,
    }
    http = fake(
        {
            **DEFAULT_ROUTES,
            "/emails/test@driftz.net?limit=100": {
                "success": True,
                "result": {"items": [row], "nextCursor": None, "locked": False, "isPrivate": False},
            },
        }
    )
    with pytest.raises(SchemaDrift, match="id"):
        await Driftz(http).list(Address("test@driftz.net", "driftz"))


async def test_a_null_subject_becomes_an_empty_string():
    row = {
        "id": "m1",
        "fromAddress": "noreply@example.test",
        "subject": None,
        "receivedAt": 1794740000,
        "expiresAt": 1794743600,
        "hasAttachments": False,
        "attachmentCount": 0,
    }
    http = fake(
        {
            **DEFAULT_ROUTES,
            "/emails/test@driftz.net?limit=100": {
                "success": True,
                "result": {"items": [row], "nextCursor": None, "locked": False, "isPrivate": False},
            },
        }
    )
    rows = await Driftz(http).list(Address("test@driftz.net", "driftz"))
    assert rows[0].subject == "" and rows[0].sender == "noreply@example.test"


async def test_a_locked_inbox_flags_its_rows():
    payload = {
        "success": True,
        "result": {"items": [], "nextCursor": None, "locked": True, "isPrivate": True},
    }
    http = fake({**DEFAULT_ROUTES, "/emails/test@driftz.net?limit=100": payload})
    rows = await Driftz(http).list(Address("test@driftz.net", "driftz"))
    assert rows == []  # no rows to flag, but locked inboxes must not crash


async def test_a_delivered_message_round_trips_through_list_and_get():
    # Recorded end to end on 2026-11-15: Klaviyo welcome mail landed on a
    # generated temp inbox ~2.5 min after the subscribe POST.
    listed = load("temp_list_delivered.json")
    message = load("temp_message.json")
    addr = "u3thvr9w0jbixaa0x185quyh@manornewtech.org"
    routes = {
        **DEFAULT_ROUTES,
        f"/temp/{addr}?limit=100": listed,
        f"/temp/{addr}/ol8qb66hifmj3nm61sp73rni": message,
    }
    provider = Driftz(fake(routes))
    address = Address(addr, "driftz")
    rows = await provider.list(address)
    assert rows[0].id == "ol8qb66hifmj3nm61sp73rni"
    assert rows[0].sender == "noreply@example.test"
    assert rows[0].subject == "Your verification code is 482913."
    assert rows[0].received_at is not None
    msg = await provider.get(address, rows[0].id)
    assert msg.text == "Your verification code is 482913."
    assert msg.links == ("https://example.test/verify?t=482913",)


async def test_get_routes_temp_addresses_to_the_temp_path():
    listed = {
        "success": True,
        "result": {
            "items": [
                {
                    "id": "em1",
                    "fromAddress": "noreply@example.test",
                    "subject": "Your verification code is 482913.",
                    "receivedAt": 1794740000,
                    "expiresAt": 1794743600,
                    "hasAttachments": False,
                    "attachmentCount": 0,
                }
            ],
            "nextCursor": None,
            "expiresAt": 1794743600,
        },
    }
    message = {
        "success": True,
        "result": {
            "id": "em1",
            "fromAddress": "noreply@example.test",
            "subject": "Your verification code is 482913.",
            "receivedAt": 1794740000,
            "expiresAt": 1794743600,
            "htmlContent": "<p>Your verification code is 482913.</p>",
            "textContent": "Your verification code is 482913.",
            "hasAttachments": False,
            "attachmentCount": 0,
        },
    }
    routes = {
        **DEFAULT_ROUTES,
        "/temp/u3thvr9w0jbixaa0x185quyh@manornewtech.org?limit=100": listed,
        "/temp/u3thvr9w0jbixaa0x185quyh@manornewtech.org/em1": message,
    }
    provider = Driftz(fake(routes))
    address = Address("u3thvr9w0jbixaa0x185quyh@manornewtech.org", "driftz")
    msg = await provider.get(address, "em1")
    assert msg.text == "Your verification code is 482913."
    assert msg.links == ()


async def test_get_routes_permanent_addresses_to_inbox_by_id():
    message = {
        "success": True,
        "result": {
            "id": "em2",
            "fromAddress": "noreply@example.test",
            "subject": "Your verification code is 482913.",
            "receivedAt": 1794740000,
            "expiresAt": 1794743600,
            "htmlContent": "",
            "textContent": "Your verification code is 482913.",
            "hasAttachments": False,
            "attachmentCount": 0,
        },
    }
    http = fake({**DEFAULT_ROUTES, "/inbox/em2": message})
    msg = await Driftz(http).get(Address("test@driftz.net", "driftz"), "em2")
    assert msg.id == "em2" and msg.text == "Your verification code is 482913."


async def test_an_expired_or_missing_message_is_gone_not_drift():
    http = fake(
        {**DEFAULT_ROUTES, "/inbox/em3": Reply(404, {"success": False, "error": "not found"})}
    )
    with pytest.raises(MessageGone):
        await Driftz(http).get(Address("test@driftz.net", "driftz"), "em3")


async def test_an_expired_temp_inbox_is_gone_not_provider_down():
    http = fake(
        {
            **DEFAULT_ROUTES,
            "/temp/u3thvr9w0jbixaa0x185quyh@manornewtech.org/em1": Reply(
                400, {"success": False, "error": "Inbox has expired"}
            ),
        }
    )
    with pytest.raises(MessageGone):
        await Driftz(http).get(
            Address("u3thvr9w0jbixaa0x185quyh@manornewtech.org", "driftz"), "em1"
        )


async def test_delete_is_idempotent_on_404_and_down_on_5xx():
    class StatusTransport:
        provider = "driftz"

        def __init__(self, status):
            self.status = status

        async def send(self, method, url, **kw):
            class R:
                status_code = self.status

            return R()

    address = Address("test@driftz.net", "driftz")
    assert await Driftz(StatusTransport(200)).delete(address, "em1") is None
    assert await Driftz(StatusTransport(404)).delete(address, "em1") is None
    with pytest.raises(ProviderDown):
        await Driftz(StatusTransport(500)).delete(address, "em1")


def test_caps_declare_public_inbox_and_one_hour_ttl():
    from datetime import timedelta

    caps = Driftz.__dict__["caps"]
    assert caps.inbox_privacy is InboxPrivacy.PUBLIC
    assert caps.address_ttl == timedelta(hours=1)
    assert caps.domain_count == 37
    assert len(caps.domains) == 24  # 22 public + 2 temp
    assert not caps.needs_state
