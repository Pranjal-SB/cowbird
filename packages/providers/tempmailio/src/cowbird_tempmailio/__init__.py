"""temp-mail.io — the web client's own JSON API at api.internal.temp-mail.io.

List rows carry the whole message, text and HTML both, so get() is a lookup in
list() and there is no second endpoint. The token generate() returns is only
needed to delete; reading is keyed on the address alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://api.internal.temp-mail.io/api/v3"


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(row["created_at"]).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return None


class TempMailIo(Provider):
    name = "tempmailio"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("temp-mail.io",),
        domains=(),
        domain_count=7,
        address_ttl=timedelta(days=1),
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        if opts and (opts.local or opts.domain):
            raise NotSupported("tempmailio picks both the local part and the domain")
        data = await self.http.json(
            "POST", f"{API}/email/new", json={"min_name_length": 10, "max_name_length": 10}
        )
        if not isinstance(data, dict) or not isinstance(data.get("email"), str):
            raise SchemaDrift(self.name, expected="'email' from /email/new", got=data)
        return Address(
            value=data["email"],
            provider=self.name,
            expires_at=datetime.now(UTC) + self.caps.address_ttl,
        )

    async def _rows(self, address: Address) -> list[dict]:
        rows = await self.http.json("GET", f"{API}/email/{address.value}/messages")
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and "id" in r for r in rows
        ):
            raise SchemaDrift(self.name, expected="an array of rows with 'id'", got=rows)
        return rows

    async def list(self, address: Address) -> list[MessageRow]:
        return [
            MessageRow(
                id=row["id"],
                sender=row.get("from") or "",
                subject=row.get("subject") or "",
                received_at=_at(row),
            )
            for row in await self._rows(address)
        ]

    async def get(self, address: Address, id: str) -> Message:
        row = next((r for r in await self._rows(address) if r["id"] == id), None)
        if row is None:
            raise MessageGone(f"tempmailio: message {id} is no longer listed")
        html = row.get("body_html") or ""
        text = row.get("body_text") or html_to_text(html)
        return Message(
            id=id,
            sender=row.get("from") or "",
            subject=row.get("subject") or "",
            received_at=_at(row),
            html=html,
            text=text,
            links=extract_links(html),
        )
