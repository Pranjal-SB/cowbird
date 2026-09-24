"""tempmailhub.org — real Gmail accounts, read server-side over IMAP.

Each address is a whole Gmail account from a small recycled pool, and the
inboxes are shared: an address arrives already holding strangers' mail, and
generate() hands the same account back to repeat callers from one IP. Callers
must match on sender or subject, never take "the newest message".

The web client lists by `email_id`; the same endpoint takes `{"email"}` too
and answers the same rows, so no state is carried. The rows hold the whole
body, so get() is a lookup in list(). There is no delete and no domain choice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://api.tempmailhub.org"


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(row["date"]).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return None


def _sender(row: dict) -> str:
    address, name = row.get("senderEmail") or "", row.get("senderName") or ""
    return f"{name} <{address}>" if name and address else address or name


class TempMailHub(Provider):
    name = "tempmailhub"
    caps = Capabilities(
        kind=frozenset({Kind.GMAIL_ALIAS}),
        sites=("tempmailhub.org",),
        domains=("gmail.com",),
        domain_count=1,
        # The site's terms say 15 minutes; not measured.
        address_ttl=timedelta(minutes=15),
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        # x-ratelimit-limit is 100 per window, and a listing is an IMAP fetch
        # that takes 5-10s (a stuck account hits nginx's 60s 504).
        max_concurrency=1,
        poll_interval=10.0,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        if opts and (opts.local or opts.domain):
            raise NotSupported("tempmailhub picks both the local part and the domain")
        data = await self.http.json("POST", f"{API}/emails")
        email = data.get("email") if isinstance(data, dict) else None
        if not isinstance(email, str) or "@" not in email:
            raise SchemaDrift(self.name, expected="'email' from POST /emails", got=data)
        return Address(value=email, provider=self.name)

    async def _rows(self, address: Address) -> list[dict]:
        data = await self.http.json("POST", f"{API}/emails/messages", json={"email": address.value})
        rows = data.get("emails") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("id") for r in rows):
            raise SchemaDrift(self.name, expected="'emails' rows with 'id'", got=data)
        return rows

    async def list(self, address: Address) -> list[MessageRow]:
        return [
            MessageRow(
                id=str(row["id"]),
                sender=_sender(row),
                subject=row.get("subject") or "",
                received_at=_at(row),
            )
            for row in await self._rows(address)
        ]

    async def get(self, address: Address, id: str) -> Message:
        row = next((r for r in await self._rows(address) if str(r["id"]) == id), None)
        if row is None:
            raise MessageGone(f"tempmailhub: message {id} is no longer listed")
        html = row.get("body") or ""
        return Message(
            id=id,
            sender=_sender(row),
            subject=row.get("subject") or "",
            received_at=_at(row),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
