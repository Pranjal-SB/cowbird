"""mail.cx — the site's own origin, not the keyed api.mail.cx host.

The inbox endpoint is a long-poll: an empty inbox holds about 25 seconds and
answers 204, and it returns the moment mail lands. list() is that long-poll, so
the default watch loop over it already sees mail as soon as it arrives.
"""

from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timedelta

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://mail.cx/v1"
_LOCAL = string.ascii_lowercase + string.digits


def _at(row: dict) -> datetime | None:
    raw = row.get("created_at")
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


class MailCx(Provider):
    name = "mailcx"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("mail.cx",),
        domains=(),
        domain_count=3,
        address_ttl=None,
        # The config's ttl_seconds, read as message retention.
        message_ttl=timedelta(hours=1),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        # ponytail: one request at a time. Concurrent long-polls draw 429, so a
        # second address waits behind the first one's 25 s hold.
        max_concurrency=1,
        poll_interval=1.0,
    )

    def __init__(self, http) -> None:
        super().__init__(http)
        self._domains: tuple[str, ...] | None = None
        self._default: str | None = None

    async def _load_domains(self) -> None:
        config = await self.http.json("GET", f"{API}/config")
        entries = config.get("system_domains") if isinstance(config, dict) else None
        if not isinstance(entries, list) or not entries:
            raise SchemaDrift(self.name, expected="system_domains in /v1/config", got=config)
        dict_entries = [e for e in entries if isinstance(e, dict)]
        domains = tuple(e["domain"] for e in dict_entries if "domain" in e)
        if not domains:
            raise SchemaDrift(self.name, expected="'domain' in a system_domains entry", got=entries)
        self._domains = domains
        default = next((e["domain"] for e in dict_entries if e.get("default")), None)
        self._default = default if default is not None else domains[0]

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if self._domains is None:
            await self._load_domains()
        if opts.domain and opts.domain not in self._domains:
            raise NotSupported(f"mailcx does not serve {opts.domain}; has {self._domains}")
        local = opts.local or "".join(secrets.choice(_LOCAL) for _ in range(6))
        return Address(value=f"{local}@{opts.domain or self._default}", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        # ponytail: a quick peek at an empty inbox costs the full 25 s hold.
        # No parameter makes this endpoint non-blocking (measured 2026-09-11).
        resp = await self.http.send("GET", f"{API}/inbox/{address.value}")
        if resp.status_code == 204:
            return []
        if resp.status_code >= 400:
            raise ProviderDown(f"mailcx: inbox answered HTTP {resp.status_code}")
        try:
            payload = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        rows = payload.get("emails") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="a list under 'emails'", got=payload)
        out = []
        for row in rows:
            if not isinstance(row, dict) or "id" not in row:
                got = list(row) if isinstance(row, dict) else row
                raise SchemaDrift(self.name, expected="'id' in an inbox row", got=got)
            out.append(
                MessageRow(
                    id=row["id"],
                    sender=row.get("from_email", ""),
                    subject=row.get("subject", ""),
                    received_at=_at(row),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        resp = await self.http.send("GET", f"{API}/email/{id}")
        if resp.status_code == 404:
            raise MessageGone(f"mailcx: message {id} is gone")
        if resp.status_code >= 400:
            raise ProviderDown(f"mailcx: email answered HTTP {resp.status_code}")
        try:
            row = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        if not isinstance(row, dict):
            raise SchemaDrift(self.name, expected="an object from /v1/email/{id}", got=row)
        html = row.get("html_body") or ""
        return Message(
            id=row.get("id", id),
            sender=row.get("from_email", ""),
            subject=row.get("subject", ""),
            received_at=_at(row),
            html=html,
            text=row.get("text_body") or html_to_text(html),
            links=extract_links(html),
        )
