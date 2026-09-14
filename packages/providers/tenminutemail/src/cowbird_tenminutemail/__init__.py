"""10minutemail.com.

The inbox is the JSESSIONID cookie: on a shared cookie jar a second generate()
hands back the first address. So each request runs on a fresh session and the
cookie travels in Address.state. Messages come back whole in the list call;
there is no separate read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

BASE = "https://10minutemail.com"
_COOKIE = "JSESSIONID"


def _at(row: dict) -> datetime | None:
    raw = row.get("sentDate")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class TenMinuteMail(Provider):
    name = "10minutemail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("10minutemail.com",),
        domains=(),
        domain_count=1,
        address_ttl=timedelta(minutes=10),
        message_ttl=timedelta(minutes=10),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        fresh_session=True,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        resp = await self.http.send("GET", f"{BASE}/session/address")
        if resp.status_code >= 400:
            raise ProviderDown(f"10minutemail: /session/address answered HTTP {resp.status_code}")
        try:
            payload = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        session = resp.cookies.get(_COOKIE)
        if not session:
            raise SchemaDrift(
                self.name, expected=f"a {_COOKIE} cookie from /session/address",
                got=list(resp.cookies),
            )
        if not isinstance(payload, dict) or "address" not in payload:
            raise SchemaDrift(self.name, expected="'address' in /session/address", got=payload)
        return Address(
            value=payload["address"],
            provider=self.name,
            expires_at=datetime.now(UTC) + self.caps.address_ttl,
            state=session,
        )

    async def _rows(self, address: Address) -> list[dict]:
        if not address.state:
            raise NotSupported("10minutemail needs the session issued by generate()")
        resp = await self.http.send(
            "GET", f"{BASE}/messages/messagesAfter/0", cookies={_COOKIE: address.state}
        )
        if resp.status_code >= 400:
            raise ProviderDown(
                f"10minutemail: /messages/messagesAfter/0 answered HTTP {resp.status_code}"
            )
        try:
            payload = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        if not isinstance(payload, list):
            raise SchemaDrift(self.name, expected="a JSON array of messages", got=payload)
        for row in payload:
            if not isinstance(row, dict) or "id" not in row:
                raise SchemaDrift(self.name, expected="'id' in a message row", got=row)
        return payload

    async def list(self, address: Address) -> list[MessageRow]:
        return [
            MessageRow(
                id=str(row["id"]),
                sender=row.get("sender") or "",
                subject=row.get("subject") or "",
                received_at=_at(row),
            )
            for row in await self._rows(address)
        ]

    async def get(self, address: Address, id: str) -> Message:
        for row in await self._rows(address):
            if str(row["id"]) == id:
                html = row.get("bodyHtmlContent") or ""
                return Message(
                    id=id,
                    sender=row.get("sender") or "",
                    subject=row.get("subject") or "",
                    received_at=_at(row),
                    html=html,
                    text=row.get("bodyPlainText") or html_to_text(html),
                    links=extract_links(html),
                )
        raise MessageGone(f"10minutemail: message {id} is no longer listed")
