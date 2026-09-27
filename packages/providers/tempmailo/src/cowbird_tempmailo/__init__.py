"""tempmailo.com, an ASP.NET app driven by its own page script.

The home page carries an antiforgery token in a hidden input and sets the
cookie it is bound to; every API call sends the token as the
`RequestVerificationToken` header with that cookie, or gets a bare 400.

GET /changemail mints an address and answers it as plain text. POST / with
{"mail": address} lists the inbox, and the address is the only key: any
antiforgery pair reads any address, and an address the site never issued
answers [] like an empty one. Inboxes are public to anyone who knows the
address, so there is no state to carry.

Rows carry the body, text and HTML both, so get() reads the listing.
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from cowbird.errors import MessageGone, NotSupported, RateLimited, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

BASE = "https://tempmailo.com"
_TOKEN = re.compile(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"')
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+")


class TempMailo(Provider):
    name = "tempmailo"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("tempmailo.com",),
        # Sampled 20 addresses: forexzig.com, denipl.com, fxzig.com, denipl.net.
        # Not declared, since the sample may be short of the pool.
        domains=(),
        domain_count=4,
        address_ttl=None,
        # "We keep the received emails for two days from the date of receipt."
        message_ttl=timedelta(days=2),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        # About 20 new addresses per IP before "Rate limit exceeded!".
        max_concurrency=2,
        # The site's own page polls every 16 seconds.
        poll_interval=15.0,
    )

    async def _call(self, method: str, url: str, **kw: Any) -> str:
        """One API call carrying a fresh token and the cookie it is bound to."""
        home = await self.http.send("GET", f"{BASE}/")
        token = _TOKEN.search(home.text)
        if not token:
            raise SchemaDrift(
                self.name, expected="an antiforgery token from /", got=home.text[:200]
            )
        # The site sets its antiforgery cookie once; after that the transport's
        # cookie jar carries it and the home page sends no Set-Cookie.
        resp = await self.http.send(
            method,
            url,
            headers={"RequestVerificationToken": token.group(1)},
            cookies=dict(home.cookies) or None,
            **kw,
        )
        if resp.status_code == 400 and "rate limit" in resp.text.lower():
            raise RateLimited(f"{self.name}: {resp.text[:100]}")
        if resp.status_code != 200:
            raise SchemaDrift(
                self.name, expected="HTTP 200", got=f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.text

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        if opts and (opts.local or opts.domain):
            raise NotSupported("tempmailo picks both the local part and the domain")
        value = (
            await self._call("GET", f"{BASE}/changemail", params={"_r": str(random.random())})
        ).strip()
        if not _ADDRESS.fullmatch(value):
            raise SchemaDrift(self.name, expected="an address from /changemail", got=value[:200])
        return Address(value=value, provider=self.name)

    async def _rows(self, address: Address) -> list[dict]:
        text = await self._call("POST", f"{BASE}/", json={"mail": address.value})
        try:
            rows = json.loads(text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=text[:200]) from exc
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and isinstance(r.get("id"), str) for r in rows
        ):
            raise SchemaDrift(self.name, expected="an array of rows with 'id'", got=rows)
        return rows

    def _row(self, r: dict) -> MessageRow:
        when = r.get("date")
        try:
            received = None if when is None else datetime.fromisoformat(when).astimezone(UTC)
        except (TypeError, ValueError) as exc:
            raise SchemaDrift(self.name, expected="an ISO 'date'", got=when) from exc
        return MessageRow(
            id=r["id"],
            sender=r.get("from") or "",
            subject=r.get("subject") or "",
            received_at=received,
        )

    async def list(self, address: Address) -> list[MessageRow]:
        return [self._row(r) for r in await self._rows(address)]

    async def get(self, address: Address, id: str) -> Message:
        raw = next((r for r in await self._rows(address) if r["id"] == id), None)
        if raw is None:
            raise MessageGone(f"tempmailo: message {id} is no longer listed")
        html, text = raw.get("html") or "", raw.get("text") or ""
        if not isinstance(html, str) or not isinstance(text, str):
            raise SchemaDrift(self.name, expected="'html' and 'text' as strings", got=raw)
        row = self._row(raw)
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=text or html_to_text(html),
            links=extract_links(html),
        )
