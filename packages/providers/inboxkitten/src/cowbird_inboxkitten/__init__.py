"""inboxkitten.com — open source (uilicious/inboxkitten), Mailgun-backed.

Any local part at the domain receives, so generate() makes no request. A read
needs both the Mailgun storage key and its region, so the message id packs them
as "region:key".
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://inboxkitten.com/api/v1/mail"
DOMAIN = "inboxkitten.com"


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(row["timestamp"]), UTC)
    except (KeyError, TypeError, ValueError, OSError):
        return None


class InboxKitten(Provider):
    name = "inboxkitten"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("inboxkitten.com",),
        domains=(DOMAIN,),
        domain_count=1,
        address_ttl=None,
        # Not measured. Mailgun storage expiry is what makes an old row
        # unreadable; one day is a conservative reading of it.
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain != DOMAIN:
            raise NotSupported(f"inboxkitten only serves {DOMAIN}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        return Address(value=f"{local}@{DOMAIN}", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        local = address.value.split("@", 1)[0]
        rows = await self.http.json("GET", f"{API}/list", params={"recipient": local})
        if not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="a JSON array from /list", got=rows)
        out = []
        for row in rows:
            storage = row.get("storage") if isinstance(row, dict) else None
            if not isinstance(storage, dict) or not {"key", "region"} <= storage.keys():
                raise SchemaDrift(
                    self.name, expected="storage.key and storage.region in a row", got=row
                )
            headers = (row.get("message") or {}).get("headers") or {}
            out.append(
                MessageRow(
                    id=f"{storage['region']}:{storage['key']}",
                    sender=headers.get("from", ""),
                    subject=headers.get("subject", ""),
                    received_at=_at(row),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        region, sep, key = id.partition(":")
        if not sep or not key:
            raise NotSupported(f"inboxkitten message ids are region:key, got {id!r}")
        params = {"key": key, "region": region}
        try:
            html = await self.http.text("GET", f"{API}/getHtml", params=params)
            info = await self.http.json("GET", f"{API}/getInfo", params=params)
        except ProviderDown:
            # A read 500s when Mailgun has expired the stored message under a
            # row that still lists. If the list answers, the backend is fine
            # and this message is gone; if it does not, this raises instead.
            await self.list(address)
            raise MessageGone(f"inboxkitten: message {id} expired upstream") from None
        if not isinstance(info, dict):
            raise SchemaDrift(self.name, expected="an object from /getInfo", got=info)
        return Message(
            id=id,
            sender=(info.get("emailAddress") or "").strip(" <>"),
            subject=info.get("subject", ""),
            received_at=None,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
