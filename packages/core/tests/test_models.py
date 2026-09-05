from datetime import timedelta

import pytest
from cowbird.models import Address, Capabilities, Kind


def make_caps(**kw) -> Capabilities:
    base = {
        "kind": frozenset({Kind.OWN_DOMAIN}),
        "sites": ("mail.tm",),
        "domains": ("indigo.com",),
        "domain_count": 1,
        "address_ttl": None,
        "message_ttl": timedelta(days=7),
        "push": False,
        "delete": True,
        "custom_local": False,
        "self_hosted": False,
        "needs_residential_ip": False,
    }
    return Capabilities(**{**base, **kw})


def test_capabilities_are_immutable():
    caps = make_caps()
    with pytest.raises(AttributeError):
        caps.push = True


def test_kind_is_a_set_so_one_backend_can_serve_two_kinds():
    caps = make_caps(kind=frozenset({Kind.GMAIL_ALIAS, Kind.OUTLOOK_ALIAS}))
    assert caps.serves(Kind.GMAIL_ALIAS)
    assert caps.serves(Kind.OUTLOOK_ALIAS)
    assert not caps.serves(Kind.EDU)


def test_address_without_ttl_never_expires():
    addr = Address(value="a@b.c", provider="mailtm", expires_at=None)
    assert addr.is_expired() is False


def test_address_state_is_opaque_and_kept_out_of_repr():
    # It holds credentials. It must not turn up in a log line or a traceback.
    addr = Address(value="a@b.c", provider="mailtm", state='{"password": "hunter2"}')
    assert "hunter2" not in repr(addr)


def test_capabilities_carry_a_concurrency_budget_and_a_poll_cadence():
    caps = make_caps(max_concurrency=8, poll_interval=15.0)
    assert caps.max_concurrency == 8
    assert caps.poll_interval == 15.0
