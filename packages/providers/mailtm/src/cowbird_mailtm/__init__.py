from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from cowbird.errors import NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://api.mail.tm"


def _pack(address: str, password: str, token: str) -> str:
    """mail.tm's JWT expires, and re-minting it needs the credentials that
    created the account. All three travel together in Address.state, which core
    treats as an opaque string."""
    return json.dumps({"address": address, "password": password, "token": token})


def _unpack(state: str | None) -> dict[str, str]:
    if not state:
        raise NotSupported("mailtm needs the state issued by generate()")
    return json.loads(state)


def _members(payload: object, where: str) -> list:
    """mail.tm speaks JSON-LD. A missing hydra:member means the shape moved."""
    if not isinstance(payload, dict) or "hydra:member" not in payload:
        keys = list(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SchemaDrift("mailtm", expected=f"hydra:member in {where}", got=keys)
    return payload["hydra:member"]


def _at(row: dict) -> datetime | None:
    raw = row.get("createdAt")
    return datetime.fromisoformat(raw) if raw else None


class MailTm(Provider):
    name = "mailtm"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("mail.tm",),
        domains=(),  # discovered per call; mail.tm rotates them
        domain_count=1,
        address_ttl=None,
        message_ttl=timedelta(days=7),
        push=False,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        domains_payload = await self.http.json("GET", f"{API}/domains")
        domains = [d["domain"] for d in _members(domains_payload, "domains")]
        if opts.domain and opts.domain not in domains:
            # A caller asking for a domain this backend does not serve is a caller
            # error, not upstream drift. Raising SchemaDrift here would quarantine
            # a perfectly healthy provider because someone passed a typo.
            raise NotSupported(f"mailtm does not serve {opts.domain}; has {domains}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        value = f"{local}@{opts.domain or domains[0]}"
        password = secrets.token_urlsafe(16)

        await self.http.json(
            "POST", f"{API}/accounts", json={"address": value, "password": password}
        )
        auth = await self.http.json(
            "POST", f"{API}/token", json={"address": value, "password": password}
        )
        if "token" not in auth:
            raise SchemaDrift("mailtm", expected="token in /token", got=list(auth))
        state = _pack(value, password, auth["token"])
        return Address(value=value, provider=self.name, state=state)

    def _auth(self, address: Address) -> dict[str, str]:
        return {"Authorization": f"Bearer {_unpack(address.state)['token']}"}

    async def list(self, address: Address) -> list[MessageRow]:
        payload = await self.http.json("GET", f"{API}/messages", headers=self._auth(address))
        return [
            MessageRow(
                id=row["id"],
                sender=row.get("from", {}).get("address", ""),
                subject=row.get("subject", ""),
                received_at=_at(row),
            )
            for row in _members(payload, "messages")
        ]

    async def get(self, address: Address, id: str) -> Message:
        row = await self.http.json("GET", f"{API}/messages/{id}", headers=self._auth(address))
        if "id" not in row:
            raise SchemaDrift("mailtm", expected="id in a message document", got=list(row))
        html = "".join(row.get("html") or [])
        return Message(
            id=row["id"],
            sender=row.get("from", {}).get("address", ""),
            subject=row.get("subject", ""),
            received_at=_at(row),
            html=html,
            text=row.get("text") or html_to_text(html),
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        await self.http.json("DELETE", f"{API}/messages/{id}", headers=self._auth(address))
