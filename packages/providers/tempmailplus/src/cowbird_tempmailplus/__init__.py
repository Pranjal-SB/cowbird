"""tempmail.plus — the web client's open `/api/mails` endpoints.

No account and no token: any local part at a served domain receives, and the
inbox is read by the address alone. The optional `epin` guards a box someone
has locked with a PIN; a fresh address never has one.

The domain list is only in the page markup, not an API, so it is kept here.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://tempmail.plus/api/mails"
DOMAINS = (
    "mailto.plus",
    "fexpost.com",
    "fexbox.org",
    "mailbox.in.ua",
    "rover.info",
    "chitthi.in",
    "fextemp.com",
    "any.pink",
    "merepost.com",
)


def _at(value: object) -> datetime | None:
    try:
        return parsedate_to_datetime(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


class TempMailPlus(Provider):
    name = "tempmailplus"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("tempmail.plus",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        # Not measured.
        address_ttl=timedelta(days=1),
        message_ttl=timedelta(days=1),
        push=False,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    def _box(self, address: Address) -> dict[str, str]:
        return {"email": address.value, "epin": ""}

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in DOMAINS:
            raise NotSupported(f"tempmailplus does not serve {opts.domain}")
        domain = opts.domain or secrets.choice(DOMAINS)
        local = opts.local or f"cb{secrets.token_hex(5)}"
        return Address(value=f"{local}@{domain}", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        data = await self.http.json("GET", API, params={**self._box(address), "limit": 20})
        rows = data.get("mail_list") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and "mail_id" in r for r in rows
        ):
            raise SchemaDrift(self.name, expected="'mail_list' rows with 'mail_id'", got=data)
        return [
            MessageRow(
                id=str(row["mail_id"]),
                sender=row.get("from_mail") or "",
                subject=row.get("subject") or "",
                # The listing's "time" is the server's local clock with no
                # zone; get() has the real Date header.
                received_at=None,
            )
            for row in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        data = await self.http.json("GET", f"{API}/{id}", params=self._box(address))
        if not isinstance(data, dict):
            raise SchemaDrift(self.name, expected="a message object", got=data)
        # Unknown ids and ids from another box both answer {"result": false}.
        if data.get("result") is False:
            raise MessageGone(f"tempmailplus: message {id} is gone")
        html = data.get("html") or ""
        text = data.get("text") or html_to_text(html)
        return Message(
            id=id,
            sender=data.get("from") or data.get("from_mail") or "",
            subject=data.get("subject") or "",
            received_at=_at(data.get("date")),
            html=html,
            text=text,
            links=extract_links(html or text),
        )

    async def delete(self, address: Address, id: str) -> None:
        data = await self.http.json("DELETE", f"{API}/{id}", data=self._box(address))
        # Deleting a message that is already gone answers false too; the caller
        # wanted it gone, and it is.
        if not isinstance(data, dict) or "result" not in data:
            raise SchemaDrift(self.name, expected="{'result': bool} from delete", got=data)
