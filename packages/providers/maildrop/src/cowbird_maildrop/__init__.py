"""maildrop.cc — a GraphQL API with introspection left on.

Any local part at the domain receives, so generate() makes no request. The
message's `data` field is the raw RFC822 source; text comes from `html`.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://api.maildrop.cc/graphql"
DOMAIN = "maildrop.cc"
_INBOX = (
    "query ($mailbox: String!) "
    "{ inbox(mailbox: $mailbox) { id headerfrom subject date } }"
)
_MESSAGE = (
    "query ($mailbox: String!, $id: String!) "
    "{ message(mailbox: $mailbox, id: $id) { id headerfrom subject date html } }"
)


def _at(row: dict) -> datetime | None:
    raw = row.get("date")
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


class Maildrop(Provider):
    name = "maildrop"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("maildrop.cc",),
        domains=(DOMAIN,),
        domain_count=1,
        address_ttl=None,
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    async def _query(self, query: str, field: str, **variables: str) -> object:
        payload = await self.http.json(
            "POST", API, json={"query": query, "variables": variables}
        )
        if not isinstance(payload, dict):
            raise SchemaDrift(
                self.name,
                expected=f"dict with data.{field}",
                got=type(payload).__name__,
            )
        # Handle GraphQL errors array: backend answer, not shape change
        if payload.get("errors"):
            # Defensively extract error message; never leak bare exceptions
            errors = payload["errors"]
            error_msg = "Unknown error"
            if isinstance(errors, list) and len(errors) > 0:
                first_error = errors[0]
                if isinstance(first_error, dict):
                    error_msg = first_error.get("message", "Unknown error")
                else:
                    error_msg = f"malformed error: {repr(first_error)}"
            else:
                error_msg = f"malformed errors: {repr(errors)}"
            raise ProviderDown(f"maildrop: {error_msg}")
        # Handle missing data key: shape change
        if "data" not in payload:
            raise SchemaDrift(
                self.name, expected=f"data.{field} from the GraphQL API", got=list(payload)
            )
        data = payload["data"] or {}
        if field not in data:
            raise SchemaDrift(self.name, expected=f"data.{field}", got=list(data))
        return data[field]

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain != DOMAIN:
            raise NotSupported(f"maildrop only serves {DOMAIN}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        return Address(value=f"{local}@{DOMAIN}", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        rows = await self._query(_INBOX, "inbox", mailbox=address.value.split("@", 1)[0])
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="a list under data.inbox", got=rows)
        out = []
        for row in rows:
            # Guard shape first: non-dict row is a schema drift, not a TypeError
            if not isinstance(row, dict) or "id" not in row:
                got = type(row).__name__ if not isinstance(row, dict) else list(row)
                raise SchemaDrift(self.name, expected="'id' in an inbox row", got=got)
            out.append(
                MessageRow(
                    id=row["id"],
                    sender=row.get("headerfrom", ""),
                    subject=row.get("subject", ""),
                    received_at=_at(row),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        row = await self._query(
            _MESSAGE, "message", mailbox=address.value.split("@", 1)[0], id=id
        )
        if row is None:
            raise MessageGone(f"maildrop: message {id} is gone")
        # Guard shape: non-dict is schema drift, not AttributeError
        if not isinstance(row, dict):
            raise SchemaDrift(
                self.name, expected="dict message object", got=type(row).__name__
            )
        html = row.get("html") or ""
        return Message(
            id=row.get("id", id),
            sender=row.get("headerfrom", ""),
            subject=row.get("subject", ""),
            received_at=_at(row),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
