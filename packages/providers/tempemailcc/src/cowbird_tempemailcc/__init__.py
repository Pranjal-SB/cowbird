"""tempemail.cc — same-origin JSON REST behind a Next.js front end.

One domain (icmans.com), one JWT per account, no captcha. `POST /api/accounts`
mints an Ed25519 JWT that never expires (the payload carries `iat` but no
`exp`, and `GET /api/me` answers `expiresAt: 0`), so the token alone is the
mailbox identity: Address.state carries it, and callers resuming out of
process need nothing else.

Every answer sits in a `{code, message, data}` envelope; `code != 200` is how
this backend says "no" (e.g. `400 password must contain at least 1 number`).
Message bodies are not in the detail document — they live at
`/api/messages/{id}/body`, served as `application/gzip` HTML regardless of
Accept-Encoding, so the adapter gunzips it itself.

Fixtures were recorded through a real `Transport` (2026-11-15) with senders
neutralized; delivery was proven live with a Klaviyo store embed.
"""

from __future__ import annotations

import gzip
import json
import secrets
import string
import zlib
from datetime import UTC, datetime
from typing import ClassVar

from cowbird.errors import (
    MessageGone,
    NotSupported,
    ProviderDown,
    RateLimited,
    SchemaDrift,
)
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://www.tempemail.cc"
PAGE = 50

# The create endpoint rejects passwords without a digit; urlsafe alone misses
# that about one time in five, so the digit is appended deterministically.
_ALPHABET = string.ascii_letters + string.digits


def _envelope(payload: object, where: str) -> dict:
    """Unwrap `{code, message, data}`; anything else is schema drift."""
    if not isinstance(payload, dict) or "code" not in payload or "data" not in payload:
        raise SchemaDrift(
            "tempemailcc", expected="{code, message, data} from " + where, got=payload
        )
    if payload["code"] == 429:
        # The create cooldown answers HTTP 200 with envelope code 429
        # ("Too many registration attempts") — a rate limit, not drift.
        raise RateLimited("tempemailcc: envelope code 429 from " + where)
    if payload["code"] != 200:
        raise SchemaDrift(
            "tempemailcc", expected="code 200 from " + where, got=payload["code"]
        )
    return payload["data"]


def _field(row: dict, key: str, where: str) -> str:
    if key not in row:
        raise SchemaDrift("tempemailcc", expected=f"{key!r} in {where}", got=list(row))
    return row[key]


def _at(epoch: object) -> datetime | None:
    """`createdAt` is seconds since the epoch, or absent."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC) if epoch else None
    except (TypeError, ValueError, OSError):
        return None


def _sender(row: dict) -> str:
    sender = row.get("from")
    return sender.get("address", "") if isinstance(sender, dict) else ""


def _unpack(state: str | None) -> dict:
    try:
        packed = json.loads(state or "")
        packed["token"]  # noqa: B018 - the one key every call needs
    except (ValueError, TypeError, KeyError) as exc:
        raise NotSupported("tempemailcc needs the state issued by generate()") from exc
    return packed


def _auth(address: Address) -> dict[str, str]:
    return {"Authorization": f"Bearer {_unpack(address.state)['token']}"}


class TempEmailCc(Provider):
    name = "tempemailcc"
    api: ClassVar[str] = API
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("www.tempemail.cc",),
        domains=("icmans.com",),
        domain_count=1,
        # The JWT has no exp and /api/me answers expiresAt: 0; both mailboxes
        # and messages claim to persist indefinitely. Nothing observed
        # contradicts that, but nothing upstream documents it either — None
        # (unknown) rather than a promise we cannot check.
        address_ttl=None,
        message_ttl=None,
        push=False,
        delete=False,  # no DELETE route in the traced API
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        inbox_privacy=InboxPrivacy.PRIVATE,  # the inbox is JWT-gated
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in self.caps.domains:
            # A caller asking for a domain this backend does not serve is a
            # caller error, not upstream drift.
            raise NotSupported(
                f"{self.name} does not serve {opts.domain}; has {list(self.caps.domains)}"
            )
        local = opts.local or f"cb{''.join(secrets.choice(_ALPHABET) for _ in range(10))}"
        email = f"{local}@{opts.domain or self.caps.domains[0]}"
        password = "".join(secrets.choice(_ALPHABET) for _ in range(16)) + "7"

        data = _envelope(
            await self.http.json(
                "POST", f"{self.api}/api/accounts", json={"email": email, "password": password}
            ),
            "POST /api/accounts",
        )
        if "token" not in data:
            raise SchemaDrift("tempemailcc", expected="'token' in create", got=list(data))
        return Address(
            value=email,
            provider=self.name,
            state=json.dumps({"token": data["token"], "password": password}),
        )

    async def list(self, address: Address) -> list[MessageRow]:
        rows: list[MessageRow] = []
        # Cursor pagination: walk nextCursor until the backend says stop. A
        # page without `items` or `pagination` is drift; so is a cursor loop.
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            query = f"?limit={PAGE}" + (f"&cursor={cursor}" if cursor else "")
            data = _envelope(
                await self.http.json(
                    "GET", f"{self.api}/api/messages{query}", headers=_auth(address)
                ),
                "GET /api/messages",
            )
            if "items" not in data or "pagination" not in data:
                raise SchemaDrift(
                    "tempemailcc",
                    expected="{items, pagination} from /api/messages",
                    got=list(data),
                )
            for row in data["items"]:
                rows.append(
                    MessageRow(
                        id=_field(row, "id", "a message row"),
                        sender=_sender(row),
                        subject=row.get("subject", ""),
                        received_at=_at(row.get("createdAt")),
                    )
                )
            cursor = data["pagination"].get("nextCursor")
            if not cursor or cursor in seen_cursors:
                return rows
            seen_cursors.add(cursor)

    async def get(self, address: Address, id: str) -> Message:
        data = _envelope(
            await self.http.json(
                "GET", f"{self.api}/api/messages/{id}", headers=_auth(address)
            ),
            f"GET /api/messages/{id}",
        )
        # The body is a separate endpoint, served as gzip-compressed HTML no
        # matter what the client offers (observed 2026-11-15: application/gzip
        # with Accept-Encoding: gzip already sent — curl_cffi leaves it alone
        # because the content type is not text/*).
        resp = await self.http.send(
            "GET", f"{self.api}/api/messages/{id}/body", headers=_auth(address)
        )
        if resp.status_code == 404:
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code >= 300:
            raise ProviderDown(f"{self.name}: body read failed, HTTP {resp.status_code}")
        raw = resp.content if hasattr(resp, "content") else resp.text.encode()
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError, zlib.error) as exc:
                # A truncated or corrupt body is upstream drift, not a caller
                # error — let the pool quarantine/reroute on it.
                raise SchemaDrift(
                    "tempemailcc",
                    expected="a valid gzip HTML body",
                    got=f"{len(raw)} gzip bytes",
                ) from exc
        html = raw.decode("utf-8", "replace")
        return Message(
            id=_field(data, "id", "a message document"),
            sender=_sender(data),
            subject=data.get("subject", ""),
            received_at=_at(data.get("createdAt")),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
