"""driftz.net provider — a documented JSON REST API, not an HTML tier.

The web app (driftz.net/temp) is a React SPA over `https://api.driftz.net`,
whose full OpenAPI spec lives at `GET /openapi/json`. This adapter follows
that spec; every response is an envelope `{"success": true, "result": {...}}`,
so a missing envelope or a missing field inside `result` is schema drift,
never a bare KeyError.

Two inbox classes:
- **Temp inboxes** (`POST /temp/generate`): random address on one of two temp
  domains, 1-hour TTL, no auth, read via `GET /temp/{address}[/{emailId}]`.
- **Permanent inboxes** (`GET /emails/{address}`): any local part on any of the
  22 public domains, pollable indefinitely — so a custom local part needs no
  HTTP call at all in generate(). Anyone with the address can read it
  (InboxPrivacy.PUBLIC); password locking is a paid feature and this adapter
  only flags locked inboxes on their rows.

`GET /health` reported `{"status": "degraded"}` on 2026-11-15: R2/attachments
are erroring upstream ("Please enable R2 through the Cloudflare Dashboard"),
so attachment endpoints are not wired — message bodies are unaffected.

Fixtures were recorded through a real `Transport` (the same client the runtime
uses), with senders/subjects neutralized.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://api.driftz.net"

# From GET /domains on 2026-11-15 (matches the OpenAPI-documented shape:
# result.public + result.temp; 13 more private domains exist but are not
# servable without payment).
PUBLIC_DOMAINS = (
    "driftz.net",
    "2thth.com",
    "aksngmail.com",
    "deislerlive.com",
    "disbs.com",
    "dryddtyioo.com",
    "eisenlog.com",
    "gmreeeddd.com",
    "gtgidhi.com",
    "hacktivc.com",
    "kshcid.com",
    "lymiddleeast.com",
    "olyanfood.com",
    "ougoods.com",
    "xnxjdjd.com",
    "yasdsgar.com",
    "guns.lat",
    "kuruptd.ink",
    "lansd.org",
    "lifetalk.us",
    "vwh.sh",
    "z44d.pro",
)
TEMP_DOMAINS = ("bbjbinin.mn", "manornewtech.org")
DOMAIN_COUNT = 37  # 22 public + 2 temp + 13 private, per /domains stats


def _at(ts: object) -> datetime | None:
    """Unix seconds → UTC. Absent or null is tolerated (subject is nullable;
    timestamps are spec-required but a None beats a crash)."""
    if not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(ts, UTC)


def _required(row: dict, key: str, where: str) -> object:
    if key not in row:
        raise SchemaDrift("driftz", expected=f"{key!r} in {where}", got=list(row))
    return row[key]


class Driftz(Provider):
    name = "driftz"
    api: ClassVar[str] = API
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("driftz.net",),
        domains=PUBLIC_DOMAINS + TEMP_DOMAINS,
        domain_count=DOMAIN_COUNT,
        address_ttl=timedelta(hours=1),  # temp inboxes; public-domain addresses poll indefinitely
        message_ttl=timedelta(hours=1),  # temp messages; permanent messages persist
        push=False,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=False,  # no auth anywhere on the public path
        inbox_privacy=InboxPrivacy.PUBLIC,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.local or opts.domain:
            # A custom local part is a permanent-address feature: the API has
            # no create call for permanent inboxes (list-any-address), but a
            # temp inbox exists only via POST /temp/generate — a "custom"
            # address on a temp domain could never receive mail. So the branch
            # is restricted to public domains, and a domain-only request gets a
            # random local (the mailtm guard pattern) instead of leaking
            # "None@domain".
            domain = opts.domain or PUBLIC_DOMAINS[0]
            if domain not in PUBLIC_DOMAINS:
                raise NotSupported(
                    f"{self.name} serves custom locals on its {len(PUBLIC_DOMAINS)} public domains"
                    f" only (temp domains: {list(TEMP_DOMAINS)})"
                )
            local = opts.local or f"cb{secrets.token_hex(5)}"
            return Address(value=f"{local}@{domain}", provider=self.name)
        payload = await self.http.json("POST", f"{self.api}/temp/generate")
        result = _envelope(payload, "POST /temp/generate")
        address = _required(result, "address", "POST /temp/generate result")
        expires = _required(result, "expiresAt", "POST /temp/generate result")
        if not isinstance(address, str):
            raise SchemaDrift("driftz", expected="a string address", got=type(address).__name__)
        return Address(
            value=address,
            provider=self.name,
            expires_at=_at(expires),
        )

    def _is_temp(self, address: Address) -> bool:
        return address.value.rsplit("@", 1)[-1] in TEMP_DOMAINS

    async def list(self, address: Address) -> list[MessageRow]:
        # `limit` is a required query param (1..100 per the OpenAPI spec); the
        # API also supports cursor pagination, but a single full page is enough
        # for inbox polling — a >100-message backlog would truncate.
        if self._is_temp(address):
            url = f"{self.api}/temp/{address.value}?limit=100"
        else:
            url = f"{self.api}/emails/{address.value}?limit=100"
        payload = await self.http.json("GET", url)
        result = _envelope(payload, "an inbox listing")
        items = _required(result, "items", "an inbox listing")
        if not isinstance(items, list):
            raise SchemaDrift("driftz", expected="items to be a list", got=type(items).__name__)
        locked = bool(result.get("locked", False))
        rows = []
        for row in items:
            # A non-dict row is the shape moving, not junk to skip: dropping it
            # silently would hide a schema change from quarantine.
            if not isinstance(row, dict):
                raise SchemaDrift("driftz", expected="a dict message row", got=type(row).__name__)
            rows.append(
                MessageRow(
                    id=str(_required(row, "id", "a message row")),
                    sender=str(row.get("fromAddress", "")),
                    subject=row.get("subject") or "",
                    received_at=_at(row.get("receivedAt")),
                    locked=locked,
                )
            )
        return rows

    async def get(self, address: Address, id: str) -> Message:
        if self._is_temp(address):
            url = f"{self.api}/temp/{address.value}/{id}"
        else:
            url = f"{self.api}/inbox/{id}"
        resp = await self.http.send("GET", url)
        # 404: email or inbox not found; 400: temp inbox has expired. Both are
        # the message's fault, not the backend's.
        if resp.status_code in (400, 404):
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code >= 400:
            raise ProviderDown(f"{self.name}: read failed, HTTP {resp.status_code}")
        payload = _envelope_json(resp.text, "a message document")
        result = _envelope(payload, "a message document")
        html = result.get("htmlContent") or ""
        text = result.get("textContent") or html_to_text(html)
        return Message(
            id=str(_required(result, "id", "a message document")),
            sender=str(result.get("fromAddress", "")),
            subject=result.get("subject") or "",
            received_at=_at(result.get("receivedAt")),
            html=html,
            text=text,
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        resp = await self.http.send("DELETE", f"{self.api}/inbox/{id}")
        if resp.status_code < 300 or resp.status_code == 404:
            # 404 is idempotent: the caller wanted the message gone and it is.
            return
        raise ProviderDown(f"{self.name}: delete failed, HTTP {resp.status_code}")


def _envelope(payload: object, where: str) -> dict:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise SchemaDrift("driftz", expected=f"success:true envelope from {where}", got=payload)
    result = payload.get("result")
    if not isinstance(result, dict):
        raise SchemaDrift("driftz", expected=f"a dict result from {where}", got=result)
    return result


def _envelope_json(text: str, where: str) -> object:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SchemaDrift(
            "driftz", expected=f"a JSON document from {where}", got=text[:200]
        ) from exc
