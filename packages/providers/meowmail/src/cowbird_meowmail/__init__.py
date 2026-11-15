"""meowmail.in — the only backend in the batch with a public OpenAPI spec.

Keyless REST: reads need no credentials, so inboxes are PUBLIC by the spec's
definition — anyone who learns the address can list and read it. The optional
API key only unlocks the 30-second long-poll (`wait=1`), which the default
polling `watch()` deliberately does not use: it would need a key the caller
never gave us. Inboxes are implicit server-side — any address at an active
domain works — so `POST /api/v1/inboxes` only guarantees an empty address.

Fixtures were recorded through a real `Transport` (Accept pinned to
application/json by `Transport.json()`), then neutralized: senders and
bodies replaced with noreply@example.test / "Your verification code is
482913.".
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

API = "meowmail.in"
BASE = "https://api.meowmail.in/api/v1"
TTL = timedelta(hours=1)
# The API's own local-part pattern: ^[a-z0-9][a-z0-9._+\-]{0,63}$
_RANDOM_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _envelope(payload: object, where: str) -> dict:
    """Every /api/v1 response is {"data": ...} — a missing or non-dict
    envelope means the shape moved."""
    if not isinstance(payload, dict) or "data" not in payload:
        got = payload if isinstance(payload, str) else type(payload).__name__
        raise SchemaDrift("meowmail", f"an envelope with 'data' from {where}", got)
    return payload


def _field(row: object, key: str, where: str) -> object:
    if not isinstance(row, dict) or key not in row:
        got = list(row) if isinstance(row, dict) else row
        raise SchemaDrift("meowmail", f"{key!r} in {where}", got)
    return row[key]


def _at(row: dict) -> datetime | None:
    raw = row.get("received_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        # A date-format change is drift like any other shape change — but a
        # timestamp we cannot parse costs nothing to degrade on (None), the
        # same convention anonymmail's _at uses.
        return None


class MeowMail(Provider):
    name = "meowmail"
    api: ClassVar[str] = BASE
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("meowmail.in", "api.meowmail.in"),
        domains=("catpost.site", "manjario.space", "meowmail.in", "meowmail.website"),
        domain_count=4,
        address_ttl=TTL,
        message_ttl=TTL,  # "roughly an hour after arrival" per the API docs
        push=False,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=False,
        max_concurrency=4,
        poll_interval=5.0,
        inbox_privacy=InboxPrivacy.PUBLIC,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain is not None and opts.domain not in self.caps.domains:
            # A caller typo, not upstream drift: SchemaDrift here would
            # quarantine a healthy provider for a fixable caller mistake.
            raise NotSupported(f"{self.name} does not serve {opts.domain}")
        local = (
            opts.local or "cb" + "".join(secrets.choice(_RANDOM_ALPHABET) for _ in range(10))
        ).lower()
        domain = opts.domain or "meowmail.in"
        payload = await self.http.json(
            "POST", f"{self.api}/inboxes", json={"domain": domain, "local_part": local}
        )
        data = _envelope(payload, "POST /inboxes")["data"]
        # The API echoes the address back, but the value is fully determined by
        # what we sent (pattern-checked local + the domain we passed), so build
        # it locally: a replayed fixture then yields distinct addresses per call.
        _field(data, "address", "POST /inboxes")
        return Address(
            value=f"{local}@{domain}", provider=self.name, expires_at=datetime.now(UTC) + TTL
        )

    async def list(self, address: Address) -> list[MessageRow]:
        payload = await self.http.json(
            "GET", f"{self.api}/inboxes/{address.value}/emails", params={"limit": 100}
        )
        data = _envelope(payload, "the inbox list")["data"]
        if not isinstance(data, list):
            raise SchemaDrift("meowmail", "a list of summaries in data", type(data).__name__)
        return [
            MessageRow(
                id=str(_field(row, "id", "a message summary")),
                sender=str(row.get("sender") or ""),
                subject=str(row.get("subject") or ""),
                received_at=_at(row),
            )
            for row in data
        ]

    async def get(self, address: Address, id: str) -> Message:
        resp = await self.http.send("GET", f"{self.api}/emails/{id}")
        if resp.status_code == 404:
            # "Returns 404 once the email expires" — roughly an hour after
            # arrival. A gone message is a correct answer, not a provider fault.
            raise MessageGone(f"{self.name}: message {id} is gone")
        try:
            payload = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift("meowmail", "a JSON message document", resp.text[:200]) from exc
        row = _envelope(payload, f"email {id}")["data"]
        _field(row, "id", f"email {id}")
        html = row.get("html_body") or ""
        text = row.get("text_body") or html_to_text(html)
        return Message(
            id=str(row["id"]),
            sender=str(row.get("sender") or ""),
            subject=row.get("subject") or "",
            received_at=_at(row),
            html=html,
            text=text,
            links=tuple(extract_links(html)),
        )

    async def delete(self, address: Address, id: str) -> None:
        resp = await self.http.send("DELETE", f"{self.api}/emails/{id}")
        if resp.status_code < 300:
            return
        if resp.status_code == 404:
            # Already gone or expired: the caller's goal is achieved, so
            # deleting twice must not raise. Deliberate idempotency.
            return
        raise ProviderDown(f"{self.name}: delete failed with HTTP {resp.status_code}")
