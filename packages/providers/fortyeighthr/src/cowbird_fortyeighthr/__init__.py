"""48hr.email — server-rendered temp mail, open source
(github.com/crazyco-xyz/48hr.email), 15 domains, 48h purge, zero auth.

Recon delta (2026-11-15): the survey described the server-rendered pages, but
the shipped source also mounts a JSON API at /api/v1 (CORS-open, 100 req/min
per IP, no auth for unlocked inboxes). This adapter uses it exclusively —
no HTML parsing anywhere:

    GET /api/v1/config/domains            {"success":true,"data":{"domains":[...]}}
    GET /api/v1/inbox/{address}           {"success":true,"data":[rows],...}
    GET /api/v1/inbox/{address}/{uid}     full message: text, html, attachments
    GET /api/v1/inbox/{address}/{uid}/raw text/plain source (not used; html+text suffice)

Every envelope is {"success": bool, "data": ...}; a missing or non-dict
envelope, or a missing "data" key, is SchemaDrift.

An inbox is any local part on any served domain — opening the page *is*
creating it. No cookies, no credentials: anyone with the address can read it
(InboxPrivacy.PUBLIC), and nothing needs carrying across processes
(needs_state=False). Addresses never expire; only messages do (48h purge).

Deletion is the one capability the JSON API does not expose: it lives on the
server-rendered side as GET /inbox/{address}/{uid}/delete, which deletes and
302s back to the inbox (curl_cffi follows the redirect, so success is 200).
"""

from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timedelta
from typing import ClassVar

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://48hr.email/api/v1"
# The server-rendered root delete() posts to. Derived from api by default so
# a self-hosted deployment (only `api` overridden) keeps delete on its own
# instance; override `web` alongside `api` if the mount point differs.
WEB = "https://48hr.email"

# The 15 domains of the official instance (also served live at
# /api/v1/config/domains; generate() asks the service rather than trusting
# this list, so a self-host or domain rotation keeps working).
DOMAINS = (
    "48hr.email", "a-germandu.de", "c1ph3r.xyz", "cloudflare.gay",
    "cutefrogs.xyz", "dontask.lol", "epstein-is.land", "free-tools.club",
    "fuck-ai.art", "gothgirl.pics", "hot-single.mom",
    "i-wont-read-your.email", "pride-worldwi.de", "sex-in-the.city",
    "used-condoms.shop",
)

_LOCAL_ALPHABET = string.ascii_lowercase + string.digits


def _envelope(payload: object, where: str) -> object:
    """Unwrap {"success": ..., "data": ...}. Anything else is the shape
    having moved, and must name both halves of the mismatch."""
    if not isinstance(payload, dict) or "data" not in payload:
        got = type(payload).__name__
        raise SchemaDrift(
            "fortyeighthr", expected=f'{{"success":..., "data":...}} from {where}', got=got
        )
    return payload["data"]


def _field(row: dict, key: str, where: str) -> object:
    """A missing required field is schema drift, not a bare KeyError: this
    names the field the backend dropped or renamed, so the quarantine issue
    is diagnosable instead of a raw stack trace."""
    if key not in row:
        raise SchemaDrift(
            "fortyeighthr", expected=f"{key!r} in {where}", got=sorted(row)
        )
    return row[key]


def _sender(row: dict) -> str:
    # Rows carry `from` as a list of {address, name}; the full message uses
    # mailparser's {value: [...]} shape. Both collapse to the first address.
    raw = row.get("from")
    entries = raw if isinstance(raw, list) else (raw or {}).get("value") or []
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        return entries[0].get("address", "") or ""
    return ""


def _at(raw: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class FortyEightHr(Provider):
    name = "fortyeighthr"
    api: ClassVar[str] = API
    web: ClassVar[str] = WEB
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("48hr.email",),
        domains=DOMAINS,
        domain_count=15,
        address_ttl=None,  # any local part works forever; only messages expire
        message_ttl=timedelta(days=2),  # 48h purge
        push=False,  # Socket.IO on the site; not usable from this client
        delete=True,
        custom_local=True,
        self_hosted=True,
        needs_residential_ip=False,
        needs_state=False,
        inbox_privacy=InboxPrivacy.PUBLIC,
    )

    async def _domains(self) -> list[str]:
        data = _envelope(
            await self.http.json("GET", f"{self.api}/config/domains"), "config/domains"
        )
        if not isinstance(data, dict) or not isinstance(data.get("domains"), list):
            raise SchemaDrift(
                "fortyeighthr", expected="{'domains': [...]} in config/domains", got=data
            )
        domains = [d for d in data["domains"] if isinstance(d, str) and d]
        if not domains:
            raise SchemaDrift("fortyeighthr", expected="non-empty domains", got=data)
        return domains

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in self.caps.domains:
            # Refused before any request: the declared list is what the pool
            # filters on, so a domain outside it needs no round trip to reject.
            raise NotSupported(
                f"{self.name} does not serve {opts.domain}; has {list(self.caps.domains)}"
            )
        domains = await self._domains()
        if opts.domain and opts.domain not in domains:
            # A caller asking for a domain this backend does not serve is a
            # caller error, not upstream drift. Raising SchemaDrift here would
            # quarantine a perfectly healthy provider because of a typo.
            raise NotSupported(
                f"{self.name} does not serve {opts.domain}; has {sorted(domains)}"
            )
        domain = opts.domain or secrets.choice(domains)
        local = opts.local or "".join(secrets.choice(_LOCAL_ALPHABET) for _ in range(10))
        value = f"{local}@{domain}".lower()
        # Opening the inbox proves the address resolves; an empty inbox is a
        # list, never an error.
        await self.list(Address(value, self.name))
        return Address(value=value, provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        data = _envelope(
            await self.http.json("GET", f"{self.api}/inbox/{address.value}"),
            "the inbox listing",
        )
        if not isinstance(data, list):
            raise SchemaDrift(
                "fortyeighthr", expected="a list of message rows in data", got=data
            )
        return [
            MessageRow(
                id=str(_field(row, "uid", "a message row")),
                sender=_sender(row),
                subject=row.get("subject", "") or "",
                received_at=_at(row.get("date")),
            )
            for row in data
            if isinstance(row, dict)
        ]

    async def get(self, address: Address, id: str) -> Message:
        resp = await self.http.send("GET", f"{self.api}/inbox/{address.value}/{id}")
        if resp.status_code == 404:
            # An expired or deleted message: the message's fault, not the
            # backend's, so it must not count against the provider's health.
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code >= 400:
            raise ProviderDown(f"{self.name}: message read answered HTTP {resp.status_code}")
        data = _envelope(_loads(resp.text), "the message document")
        if not isinstance(data, dict):
            raise SchemaDrift(
                "fortyeighthr", expected="a message document in data", got=data
            )
        html = data.get("html") or ""
        text = data.get("text") or ""
        return Message(
            id=str(_field(data, "uid", "a message document")),
            sender=_sender(data),
            subject=data.get("subject", "") or "",
            received_at=_at(data.get("date")),
            html=html,
            text=text or html_to_text(html),
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        # Deletion only exists on the server-rendered side, as a GET that
        # deletes and 302s back to the inbox. curl_cffi follows the redirect,
        # so a success arrives as a 200 inbox page; a followed redirect to the
        # error page also answers 200, but the delete itself is unconditional
        # upstream, so any <400 is success.
        resp = await self.http.send(
            "GET", f"{self.web}/inbox/{address.value}/{id}/delete"
        )
        if resp.status_code < 400:
            return
        raise ProviderDown(f"{self.name}: delete answered HTTP {resp.status_code}")


def _loads(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SchemaDrift("fortyeighthr", expected="a JSON body", got=text[:200]) from exc
