"""temporarymail.com — the web client's `/api/?action=` endpoints.

generate() hands back a secretKey, and the inbox is read with that key rather
than the address, so it travels in Address.state. A message is read by its id
alone: getEmail for the headers, /view/?i=<id> for the HTML body.

The inbox listing reports "[No Subject]" for a message nobody has opened yet;
getEmail has the real one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://www.temporarymail.com"
API = f"{SITE}/api/"


# The view page rewrites every link to its own redirector. Unwrapped, so a
# caller's link() gets the real target.
_REDIRECT = re.compile(r'href="/redirect/\?d=([^"]+)"')


def _unwrap_links(html: str) -> str:
    return _REDIRECT.sub(lambda m: f'href="{unquote(m.group(1))}"', html)


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(row["date"]), UTC)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _sender(row: dict) -> str:
    address, name = row.get("from") or "", row.get("name") or ""
    return f"{name} <{address}>" if name and address else address


class TemporaryMail(Provider):
    name = "temporarymail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("temporarymail.com",),
        domains=(),
        domain_count=9,
        # The site keeps an address alive as long as it is used once every
        # 14 days, so 14 days is the floor, not the ceiling.
        address_ttl=timedelta(days=14),
        # Not measured.
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        # A burst of calls drew a 429 during recon.
        max_concurrency=1,
    )

    async def _messages(self, action: str, value: str) -> dict:
        data = await self.http.json("GET", API, params={"action": action, "value": value})
        # An empty result is a JSON array, a non-empty one an object keyed by id.
        if data == []:
            return {}
        if not isinstance(data, dict) or not all(isinstance(r, dict) for r in data.values()):
            raise SchemaDrift(self.name, expected=f"an object of rows from {action}", got=data)
        return data

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        if opts and (opts.local or opts.domain):
            raise NotSupported("temporarymail picks both the local part and the domain")
        data = await self.http.json(
            "GET", API, params={"action": "requestEmailAccess", "key": "", "value": "random"}
        )
        if not isinstance(data, dict) or not {"address", "secretKey"} <= data.keys():
            raise SchemaDrift(
                self.name, expected="'address' and 'secretKey' from requestEmailAccess", got=data
            )
        return Address(
            value=data["address"],
            provider=self.name,
            expires_at=datetime.now(UTC) + self.caps.address_ttl,
            state=data["secretKey"],
        )

    async def list(self, address: Address) -> list[MessageRow]:
        if not address.state:
            raise NotSupported("temporarymail needs the secretKey issued by generate()")
        rows = await self._messages("checkInbox", address.state)
        return [
            MessageRow(
                id=id,
                sender=_sender(row),
                subject=row.get("subject") or "",
                received_at=_at(row),
            )
            for id, row in rows.items()
        ]

    async def get(self, address: Address, id: str) -> Message:
        row = (await self._messages("getEmail", id)).get(id)
        if row is None:
            raise MessageGone(f"temporarymail: message {id} is gone")
        html = _unwrap_links(await self.http.text("GET", f"{SITE}/view/", params={"i": id}))
        return Message(
            id=id,
            sender=_sender(row),
            subject=row.get("subject") or "",
            received_at=_at(row),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
