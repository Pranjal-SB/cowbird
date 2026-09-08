"""emailnator.com — Gmail-alias disposable addresses.

Contract verified live 2026-09-04. Ported from the standalone emailnator-api
project, which this supersedes.

Its first body read takes about 39 seconds, so poll_interval is 10.0 rather
than the 5.0 default.

It sits behind Cloudflare, but needs_residential_ip is False: the same upstream
has been served continuously from a Render dyno -- a datacenter IP -- without
drawing a challenge. Note that the declared flag is only a hint for humans;
what actually drives behaviour is the observed one on the health entry, which
HealthStore sets the moment a CloudflareChallenge is seen and clears on the
next success. Declaring True here would have been a guess overriding a
measurement.
"""

from __future__ import annotations

import json as jsonlib
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from cowbird.errors import MessageLocked, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://www.emailnator.com"

# Upstream option ids. dotGmail (3) is the default: it yields the widest pool of
# distinct-looking addresses at gmail.com.
_OPTION_IDS = {"domain": 1, "plusGmail": 2, "dotGmail": 3, "googleMail": 8}


def _at(row: dict) -> datetime | None:
    """Rows carry a Unix epoch under `timestamp` (and `date` on a full document).

    Recorded responses show plain integer seconds, not ISO strings. A bad or
    missing value yields None rather than raising: a timestamp cowbird cannot
    read is not a reason to fail a message the caller can otherwise use.
    """
    raw = row.get("timestamp", row.get("date"))
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw), UTC)
    except (ValueError, OSError, TypeError):
        return None


class Emailnator(Provider):
    name = "emailnator"
    caps = Capabilities(
        kind=frozenset({Kind.GMAIL_ALIAS}),
        sites=("emailnator.com",),
        domains=(),
        domain_count=6,
        address_ttl=None,
        message_ttl=timedelta(days=1),
        push=False,
        delete=True,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        max_concurrency=4,
        poll_interval=10.0,
        needs_state=False,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        payload = await self.http.json(
            "POST", f"{API}/api/generate-email", json={"ids": [_OPTION_IDS["dotGmail"]]}
        )
        email = payload.get("email") if isinstance(payload, dict) else None
        if not isinstance(email, str):
            raise SchemaDrift(self.name, expected="a string under 'email'", got=payload)
        return Address(value=email, provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        resp = await self.http.send(
            "POST", f"{API}/api/message-list", json={"email": address.value, "limit": 20}
        )
        # An address upstream has never seen answers 404. That is an empty
        # inbox, not a fault — every freshly generated address starts here.
        if resp.status_code == 404:
            return []
        try:
            payload = jsonlib.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        rows = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SchemaDrift(
                self.name,
                expected="a list under 'messages'",
                got=list(payload) if isinstance(payload, dict) else type(payload).__name__,
            )
        out: list[MessageRow] = []
        for row in rows:
            if "id" not in row:
                raise SchemaDrift(self.name, expected="'id' in a message row", got=list(row))
            out.append(
                MessageRow(
                    id=str(row["id"]),
                    sender=row.get("from", ""),
                    subject=row.get("subject", ""),
                    received_at=_at(row),
                    locked=bool(row.get("locked", False)),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        payload = await self.http.json("GET", f"{API}/api/message/{quote(id, safe='')}")
        if not isinstance(payload, dict) or "content" not in payload:
            raise SchemaDrift(
                self.name,
                expected="'content' in a message document",
                got=list(payload) if isinstance(payload, dict) else type(payload).__name__,
            )
        # A paywalled message answers 200 with an empty body rather than an
        # error status, so emptiness is the only signal there is.
        if payload.get("locked") or not payload["content"]:
            raise MessageLocked(f"{self.name}: message {id} is behind the paywall")
        html = payload["content"]
        # The document repeats the row's envelope fields; carrying them through
        # means a caller who only ever calls get() still gets a usable Message.
        return Message(
            id=id,
            sender=payload.get("from", ""),
            subject=payload.get("subject", ""),
            received_at=_at(payload),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        resp = await self.http.send("DELETE", f"{API}/api/delete-message/{quote(id, safe='')}")
        # Already gone is the outcome the caller wanted.
        if resp.status_code in (200, 204, 404):
            return
        raise ProviderDown(f"{self.name}: delete failed, HTTP {resp.status_code}")
