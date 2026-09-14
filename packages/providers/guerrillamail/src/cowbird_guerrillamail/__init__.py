"""guerrillamail.com.

Also served, unchanged, through cs.email and dismail.top: same endpoint, same
sid_token, addresses at guerrillamailblock.com. Those are listed as sites and
never called.

The inbox is keyed on a session cookie. On one shared cookie jar a second
generate() hands back the first address, so this provider asks for a fresh
session per request and carries the sid_token in Address.state instead.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://api.guerrillamail.com/ajax.php"
# Every call must identify a client. Neither value is checked.
_CLIENT = {"agent": "cowbird", "ip": "127.0.0.1"}
# What fetch_email answers for a message it no longer has.
_GONE = (False,)


def _field(payload: object, key: str, where: str) -> object:
    if not isinstance(payload, dict) or key not in payload:
        got = list(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SchemaDrift("guerrillamail", expected=f"{key!r} in {where}", got=got)
    return payload[key]


def _at(row: dict) -> datetime | None:
    """Epoch seconds. The welcome message carries 0, which is not a time."""
    try:
        seconds = int(row.get("mail_timestamp") or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


class GuerrillaMail(Provider):
    name = "guerrillamail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("guerrillamail.com", "sharklasers.com", "cs.email", "dismail.top"),
        domains=(),
        domain_count=11,
        address_ttl=timedelta(hours=1),
        message_ttl=timedelta(hours=1),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        fresh_session=True,
    )

    async def _call(self, function: str, sid: str | None = None, **params: object) -> object:
        query = {"f": function, **_CLIENT, **params}
        if sid is not None:
            query["sid_token"] = sid
        resp = await self.http.send("GET", API, params=query)
        if resp.status_code in (401, 403) and sid is not None:
            raise AddressExpired(f"guerrillamail: session {sid} is no longer valid")
        if resp.status_code >= 400:
            raise ProviderDown(f"guerrillamail: {function} answered HTTP {resp.status_code}")
        try:
            return json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc

    def _sid(self, address: Address) -> str:
        if not address.state:
            raise NotSupported("guerrillamail needs the sid_token issued by generate()")
        return address.state

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        payload = await self._call("get_email_address", lang="en")
        return Address(
            value=str(_field(payload, "email_addr", "get_email_address")),
            provider=self.name,
            expires_at=datetime.now(UTC) + self.caps.address_ttl,
            state=str(_field(payload, "sid_token", "get_email_address")),
        )

    async def list(self, address: Address) -> list[MessageRow]:
        payload = await self._call("check_email", self._sid(address), seq=0)
        rows = _field(payload, "list", "check_email")
        if not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="a list under 'list'", got=type(rows).__name__)
        return [
            MessageRow(
                id=str(_field(row, "mail_id", "a message row")),
                sender=row.get("mail_from", ""),
                subject=row.get("mail_subject", ""),
                received_at=_at(row),
            )
            for row in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        payload = await self._call("fetch_email", self._sid(address), email_id=id)
        if payload in _GONE:
            raise MessageGone(f"guerrillamail: message {id} is gone")
        html = str(_field(payload, "mail_body", "fetch_email") or "")
        return Message(
            id=str(_field(payload, "mail_id", "fetch_email")),
            sender=payload.get("mail_from", ""),
            subject=payload.get("mail_subject", ""),
            received_at=_at(payload),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
