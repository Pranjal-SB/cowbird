"""temp-mail.org.

10minemail.com is the same backend: its bundle decodes to web2.10minemail.com,
which serves the same routes and accepts the same tokens. The per-IP creation
limit (10 mailboxes, window unknown) is shared between the two.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://web2.temp-mail.org"


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(row["receivedAt"]), UTC)
    except (KeyError, TypeError, ValueError, OSError):
        return None


class TempMailOrg(Provider):
    name = "tempmailorg"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("temp-mail.org", "10minemail.com"),
        domains=(),
        domain_count=1,
        address_ttl=None,
        message_ttl=timedelta(hours=2),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        max_concurrency=2,
    )

    def _auth(self, address: Address) -> dict[str, str]:
        if not address.state:
            raise NotSupported("tempmailorg needs the token issued by generate()")
        return {"Authorization": f"Bearer {address.state}"}

    async def _get(self, path: str, address: Address, gone: str) -> dict:
        resp = await self.http.send("GET", f"{API}{path}", headers=self._auth(address))
        if resp.status_code == 401:
            raise AddressExpired(f"tempmailorg: {address.value} is no longer valid")
        if resp.status_code == 410:
            raise MessageGone(f"tempmailorg: {gone}")
        if resp.status_code >= 400:
            raise ProviderDown(f"tempmailorg: {path} answered HTTP {resp.status_code}")
        try:
            payload = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        if not isinstance(payload, dict):
            raise SchemaDrift(self.name, expected=f"an object from {path}", got=payload)
        return payload

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        payload = await self.http.json("POST", f"{API}/mailbox")
        if not isinstance(payload, dict) or not {"token", "mailbox"} <= payload.keys():
            raise SchemaDrift(
                self.name, expected="'token' and 'mailbox' from /mailbox", got=payload
            )
        return Address(value=payload["mailbox"], provider=self.name, state=payload["token"])

    async def list(self, address: Address) -> list[MessageRow]:
        payload = await self._get("/messages", address, gone="the mailbox is gone")
        rows = payload.get("messages")
        if not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="a list under 'messages'", got=list(payload))
        out = []
        for row in rows:
            if "_id" not in row:
                raise SchemaDrift(self.name, expected="'_id' in a message row", got=list(row))
            out.append(
                MessageRow(
                    id=row["_id"],
                    sender=row.get("from", ""),
                    subject=row.get("subject", ""),
                    received_at=_at(row),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        row = await self._get(f"/messages/{id}", address, gone=f"message {id} is gone")
        html = row.get("bodyHtml") or ""
        return Message(
            id=row.get("_id", id),
            sender=row.get("from", ""),
            subject=row.get("subject", ""),
            received_at=_at(row),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
