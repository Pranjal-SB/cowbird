"""nicemail.cc, now MailPorary. The API is Inbucket's REST shape.

The page embeds an anonymous JWT in its Nuxt payload; every API call carries it.
It is not tied to an address, so it is cached on the provider and re-scraped
when the API answers 401.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

if TYPE_CHECKING:
    from cowbird.errors import CowbirdError

SITE = "https://nicemail.cc/"
API = "https://web.mailporary.com/api/v1"
# Recorded from the page's Nuxt payload on 2026-09-11. Refresh from the page,
# not from memory.
DOMAINS = ("suarj.com", "mfxis.com", "anogz.com", "jgkcr.com", "vbgvd.com", "wzjpj.com")
_JWT = re.compile(r'"(eyJ[\w-]+\.[\w-]+\.[\w-]+)"')


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(row["posix-millis"]) / 1000, UTC)
    except (KeyError, TypeError, ValueError, OSError):
        return None


class NiceMail(Provider):
    name = "nicemail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("nicemail.cc",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        address_ttl=None,
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    def __init__(self, http) -> None:
        super().__init__(http)
        self._token: str | None = None

    async def _refresh_token(self) -> str:
        resp = await self.http.send("GET", SITE)
        if resp.status_code >= 400:
            raise ProviderDown(f"nicemail: {SITE} answered HTTP {resp.status_code}")
        page = resp.text
        match = _JWT.search(page)
        if match is None:
            raise SchemaDrift(
                self.name, expected="a JWT in the page's Nuxt payload", got=page[:200]
            )
        self._token = match.group(1)
        return self._token

    async def _get(
        self,
        path: str,
        gone: str,
        gone_error: type[CowbirdError] = MessageGone,
    ) -> object:
        for attempt in range(2):
            token = self._token if self._token and attempt == 0 else await self._refresh_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Request-ID": secrets.token_hex(16),
                "X-Timestamp": str(int(time.time())),
            }
            resp = await self.http.send("GET", f"{API}{path}", headers=headers)
            if resp.status_code != 401:
                break
            self._token = None
        if resp.status_code == 404:
            raise gone_error(f"nicemail: {gone}")
        if resp.status_code >= 400:
            raise ProviderDown(f"nicemail: {path} answered HTTP {resp.status_code}")
        try:
            return json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in DOMAINS:
            raise NotSupported(f"nicemail does not serve {opts.domain}; has {DOMAINS}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        domain = opts.domain or secrets.choice(DOMAINS)
        return Address(value=f"{local}@{domain}", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        rows = await self._get(
            f"/mailbox/{address.value}", gone="the mailbox is gone", gone_error=AddressExpired
        )
        if not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="a JSON array from /mailbox", got=rows)
        out = []
        for row in rows:
            if not isinstance(row, dict) or "id" not in row:
                got = list(row) if isinstance(row, dict) else row
                raise SchemaDrift(self.name, expected="'id' in a mailbox row", got=got)
            out.append(
                MessageRow(
                    id=row["id"],
                    sender=row.get("from", ""),
                    subject=row.get("subject", ""),
                    received_at=_at(row),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        row = await self._get(f"/mailbox/{address.value}/{id}", gone=f"message {id} is gone")
        if not isinstance(row, dict):
            raise SchemaDrift(self.name, expected="an object from /mailbox/{addr}/{id}", got=row)
        body = row.get("body")
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise SchemaDrift(self.name, expected="an object for 'body'", got=body)
        html = body.get("html") or ""
        return Message(
            id=row.get("id", id),
            sender=row.get("from", ""),
            subject=row.get("subject", ""),
            received_at=_at(row),
            html=html,
            text=body.get("text") or html_to_text(html),
            links=extract_links(html),
        )
