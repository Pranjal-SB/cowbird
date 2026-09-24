"""reusable.email — the web client's `/v1/inbox` API.

Inboxes are public: anyone can mint a read token for any name, so a caller
wanting privacy must use an unguessable local part (the default `cb<hex>` is
one). An inbox is created by `POST /v1/inbox/ensure`; reads carry
`Authorization: Inbox <token>` from `POST /v1/inbox/<address>/token`, which
lasts 24 hours.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider
from cowbird.transport import Transport

API = "https://api.reusable.email/v1/inbox"
DOMAIN = "reusable.email"


def _box(address: Address) -> str:
    return f"{API}/{quote(address.value, safe='')}"


def _status(data: object) -> object:
    """The HTTP status an error body reports: `{"error": {"status": 404, ...}}`."""
    error = data.get("error") if isinstance(data, dict) else None
    return error.get("status") if isinstance(error, dict) else None


def _at(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except ValueError:
        return None


def _sender(row: dict) -> str:
    sender = row.get("from") if isinstance(row.get("from"), dict) else {}
    address, name = sender.get("address") or "", sender.get("name") or ""
    return f"{name} <{address}>" if name and address else address


class ReusableEmail(Provider):
    name = "reusableemail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("reusable.email",),
        domains=(DOMAIN,),
        domain_count=1,
        # The name is reusable: an inbox is not retired.
        address_ttl=None,
        # Not measured; the site's agent API documents 90 days of retention.
        message_ttl=timedelta(days=90),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    def __init__(self, http: Transport) -> None:
        super().__init__(http)
        # Any process can mint a token from the address alone, so it is cached
        # here rather than carried in Address.state.
        self._tokens: dict[str, str] = {}

    async def _ensure(self, value: str) -> None:
        data = await self.http.json("POST", f"{API}/ensure", json={"address": value})
        if not isinstance(data, dict) or data.get("status") not in ("created", "exists"):
            raise SchemaDrift(self.name, expected="'created' or 'exists' from ensure", got=data)

    async def _mint(self, address: Address) -> str:
        url = f"{_box(address)}/token"
        data = await self.http.json("POST", url, json={})
        if _status(data) == 404:
            # Never ensured, e.g. a hand-made Address; the web client does the same.
            await self._ensure(address.value)
            data = await self.http.json("POST", url, json={})
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str):
            raise SchemaDrift(self.name, expected="'token' from the token endpoint", got=data)
        self._tokens[address.value] = token
        return token

    async def _read(self, address: Address, path: str = "", **kw) -> object:
        # A 401 means the cached token expired: drop it and mint once more.
        for _ in range(2):
            token = self._tokens.get(address.value) or await self._mint(address)
            data = await self.http.json(
                "GET", _box(address) + path, headers={"Authorization": f"Inbox {token}"}, **kw
            )
            if _status(data) != 401:
                return data
            self._tokens.pop(address.value, None)
        raise ProviderDown(f"{self.name}: a freshly minted inbox token was rejected")

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain != DOMAIN:
            raise NotSupported(f"reusableemail does not serve {opts.domain}")
        value = f"{opts.local or f'cb{secrets.token_hex(5)}'}@{DOMAIN}"
        await self._ensure(value)
        return Address(value=value, provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        data = await self._read(address, params={"limit": 20})
        rows = data.get("emails") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(isinstance(r, dict) and "id" in r for r in rows):
            raise SchemaDrift(self.name, expected="'emails' rows with 'id'", got=data)
        return [
            MessageRow(
                id=str(row["id"]),
                sender=_sender(row),
                subject=row.get("subject") or "",
                received_at=_at(row.get("date")),
            )
            for row in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        data = await self._read(address, f"/emails/{quote(id, safe='')}")
        # 404 for an unknown id, 400 for one that could never exist.
        if _status(data) in (400, 404):
            raise MessageGone(f"reusableemail: message {id} is gone")
        if not isinstance(data, dict) or "id" not in data:
            raise SchemaDrift(self.name, expected="a message with 'id'", got=data)
        html = data.get("html") or ""
        text = data.get("text") or html_to_text(html)
        return Message(
            id=id,
            sender=_sender(data),
            subject=data.get("subject") or "",
            received_at=_at(data.get("date")),
            html=html,
            text=text,
            links=extract_links(html or text),
        )
