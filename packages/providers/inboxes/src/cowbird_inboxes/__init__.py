"""inboxes.com — catch-all disposable addresses across 18 domains.

Contract reversed and verified live 2026-09-06/07; notes in `docs/recon/inboxes.md`.

The backend is a catch-all: any local-part on any of its domains receives mail
with no registration step. So `generate()` makes **no HTTP request at all** — it
picks a domain and returns the address. That is why `custom_local` exists, and
this is the first provider to use it.

Two consequences worth knowing. The declared `domains` tuple is the only thing
standing between a typo and addresses that silently never receive mail, since
there is no server round-trip to reject a bad one. And `/api/v2` is
unauthenticated in practice: the site's own front end attaches a Bearer token to
every call, but every endpoint answers identically without one. There is
deliberately no session handling here.

Do not reach for the v3 API or its OpenAPI spec. It looks official because it is,
but it is the paid RapidAPI surface on a different prefix behind an `apikey`
header, and the free site does not use it.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from cowbird.errors import NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://inboxes.com/api/v2"

# Recorded live from GET /api/v2/domain on 2026-09-07. The third-party inventory
# claimed 19; there are 18. Refresh with that endpoint, not from memory.
DOMAINS = (
    "blondmail.com",
    "chapsmail.com",
    "clowmail.com",
    "dropjar.com",
    "fivermail.com",
    "getairmail.com",
    "getmule.com",
    "getnada.com",
    "gimpmail.com",
    "givmail.com",
    "guysmail.com",
    "inboxbear.com",
    "replyloop.com",
    "robot-mail.com",
    "tafmail.com",
    "temptami.com",
    "tupmail.com",
    "vomoto.com",
)


def _at(row: dict) -> datetime | None:
    """Rows carry epoch seconds under `r` and an ISO string under `cr`.

    Prefer the epoch: it is unambiguous. A value cowbird cannot read yields None
    rather than raising, because an unreadable timestamp is not a reason to fail
    a message the caller can otherwise use.
    """
    raw = row.get("r")
    if raw not in (None, ""):
        try:
            return datetime.fromtimestamp(int(raw), UTC)
        except (ValueError, OSError, TypeError):
            pass
    created = row.get("cr")
    if isinstance(created, str) and created:
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


class Inboxes(Provider):
    name = "inboxes"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("inboxes.com",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        address_ttl=None,
        message_ttl=timedelta(days=7),
        push=False,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        max_concurrency=4,
        poll_interval=3.0,
        needs_state=False,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        # No network call. A catch-all backend accepts any local-part on any of
        # its domains with no registration, so asking the server for permission
        # would be a round-trip that can only fail.
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in DOMAINS:
            raise NotSupported(f"inboxes does not serve {opts.domain}; has {DOMAINS}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        return Address(
            value=f"{local}@{opts.domain or secrets.choice(DOMAINS)}", provider=self.name
        )

    async def list(self, address: Address) -> list[MessageRow]:
        payload = await self.http.json("GET", f"{API}/inbox/{address.value}")
        rows = payload.get("msgs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SchemaDrift(
                self.name,
                expected="a list under 'msgs'",
                got=list(payload) if isinstance(payload, dict) else type(payload).__name__,
            )
        out: list[MessageRow] = []
        for row in rows:
            if "uid" not in row:
                raise SchemaDrift(self.name, expected="'uid' in a message row", got=list(row))
            # Field names are terse and undocumented: uid/f/s/r. They do not
            # match the v3 spec's nicer names, so they are mapped explicitly.
            out.append(
                MessageRow(
                    id=str(row["uid"]),
                    sender=row.get("f", ""),
                    subject=row.get("s", ""),
                    received_at=_at(row),
                    locked=False,
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        payload = await self.http.json("GET", f"{API}/message/{id}")
        if not isinstance(payload, dict) or "uid" not in payload:
            raise SchemaDrift(
                self.name,
                expected="'uid' in a message document",
                got=list(payload) if isinstance(payload, dict) else type(payload).__name__,
            )
        html = payload.get("html") or ""
        # Messages carry both `text` and `html`, but `text` is frequently an
        # explicit null on HTML-only mail -- the key is present, the value is
        # not. `or` rather than a membership test: a caller handed None here
        # would see otp() fail on a message whose HTML holds the code fine.
        text = payload.get("text") or html_to_text(html)
        return Message(
            id=str(payload["uid"]),
            sender=payload.get("f", ""),
            subject=payload.get("s", ""),
            received_at=_at(payload),
            html=html,
            text=text,
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        # Bulk endpoint, ids in the body rather than the URL. Answers
        # 200 {"changed": true} whether or not the uid existed, so deleting an
        # already-gone message is success -- which is what the caller wanted.
        await self.http.send("DELETE", f"{API}/message/", json={"ids": [id]})
