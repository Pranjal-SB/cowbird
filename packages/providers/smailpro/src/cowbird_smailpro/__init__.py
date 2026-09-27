"""smailpro.com — Gmail and Outlook aliases, behind a Cloudflare Turnstile.

Creating an alias and reading a message body each need a fresh Turnstile token
in `x-captcha`; tokens are single-use, so every such call costs one solve.
Listing the inbox needs none. No csrf token, session cookie or XHR header is
checked: each call stands alone.

The inbox is opened by `{address, timestamp, key}` from create, so timestamp
and key travel in Address.state; the key is as good as a password for the box.
/app/inbox answers with a re-signed key each time, but the original keeps
working, so state never has to change. A key the site refuses comes back as
`key: null, payload: null`, which its own page reports as "This email expired!".

A sender is only a display name: api.sonjj.com drops the address from its
listing, and the body answer carries no headers.

smailpro only brokers: it signs a payload that api.sonjj.com, a separate host,
redeems for the listing or the body. A message body is fetched by address and
message id alone, without the key.

Only the free tier is requested: `username=random`, `type=alias`, server 1.
A chosen local part, a "real" account and servers 2+ are premium. Asking for
hotmail.com or any other Microsoft domain on the free tier returns an
outlook.com address, so outlook.com is the only one claimed.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from cowbird.errors import (
    AddressExpired,
    MessageGone,
    NotSupported,
    SchemaDrift,
    SolverUnavailable,
)
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://smailpro.com"
PAGE = f"{SITE}/temporary-email"
SITEKEY = "0x4AAAAAAABIS_gEec2IwOhI"
SONJJ = "https://api.sonjj.com/v1"

# Domain -> the api.sonjj.com service that reads it.
DOMAINS = {"gmail.com": "temp_gmail", "googlemail.com": "temp_gmail", "outlook.com": "temp_outlook"}
KINDS = {Kind.GMAIL_ALIAS: "gmail.com", Kind.OUTLOOK_ALIAS: "outlook.com"}


def _at(value: object) -> datetime | None:
    try:
        return parsedate_to_datetime(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _refused_token(data: object) -> None:
    """smailpro answers a Turnstile token it will not accept with
    `{"code": 403, "msg": "Captcha is invalid"}`. That is the solver's fault,
    not a change in smailpro's API, so it must not quarantine the provider."""
    if isinstance(data, dict) and "captcha" in str(data.get("msg", "")).lower():
        raise SolverUnavailable(f"smailpro refused the Turnstile token: {data.get('msg')}")


def _domain(opts: GenerateOptions) -> str:
    if opts.local:
        raise NotSupported("smailpro picks the local part on its free tier")
    if opts.kind is not None and opts.kind not in KINDS:
        raise NotSupported(f"smailpro does not serve {opts.kind}")
    domain = opts.domain or KINDS[opts.kind or Kind.GMAIL_ALIAS]
    if domain not in DOMAINS:
        raise NotSupported(f"smailpro does not serve {domain}")
    if opts.kind is not None and (domain == "outlook.com") != (opts.kind is Kind.OUTLOOK_ALIAS):
        raise NotSupported(f"{domain} is not a {opts.kind} address")
    return domain


class SmailPro(Provider):
    name = "smailpro"
    caps = Capabilities(
        kind=frozenset(KINDS),
        sites=("smailpro.com",),
        domains=tuple(DOMAINS),
        domain_count=len(DOMAINS),
        # Not measured. The key carries its creation time, so it may age out.
        address_ttl=None,
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        needs_solver=True,
        # Every create and body read waits on a solve (4.7-9.7s observed).
        max_concurrency=2,
        poll_interval=10.0,
    )

    async def _captcha(self) -> str:
        if self.http.solver is None:
            raise NotSupported(f"{self.name} needs COWBIRD_SOLVER_URL")
        return await self.http.solver.turnstile(PAGE, SITEKEY)

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        domain = _domain(opts or GenerateOptions())
        data = await self.http.json(
            "GET",
            f"{SITE}/app/create",
            params={"username": "random", "type": "alias", "domain": domain, "server": "1"},
            headers={"x-captcha": await self._captcha()},
        )
        _refused_token(data)
        if not (
            isinstance(data, dict)
            and isinstance(data.get("address"), str)
            and "@" in data["address"]
            and isinstance(data.get("timestamp"), int)
            and isinstance(data.get("key"), str)
            and data["key"]
        ):
            raise SchemaDrift(
                self.name, expected="'address', 'timestamp', 'key' from create", got=data
            )
        state = {"timestamp": data["timestamp"], "key": data["key"]}
        return Address(
            value=data["address"],
            provider=self.name,
            state=json.dumps(state, separators=(",", ":")),
        )

    def _service(self, address: Address) -> str:
        return DOMAINS.get(address.value.rpartition("@")[2].lower(), "temp_gmail")

    async def _payload(self, address: Address) -> str:
        try:
            state = json.loads(address.state or "")
            box = {"address": address.value, "timestamp": state["timestamp"], "key": state["key"]}
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported("smailpro needs the timestamp and key issued by generate()") from exc
        data = await self.http.json("POST", f"{SITE}/app/inbox", json=[box])
        entry = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if entry is None or "payload" not in entry:
            raise SchemaDrift(self.name, expected="[{'payload': ...}] from inbox", got=data)
        if entry["payload"] is None:
            raise AddressExpired(f"smailpro refused the key for {address.value}")
        if not isinstance(entry["payload"], str):
            raise SchemaDrift(self.name, expected="a string payload from inbox", got=data)
        return entry["payload"]

    async def list(self, address: Address) -> list[MessageRow]:
        payload = await self._payload(address)
        data = await self.http.json(
            "GET", f"{SONJJ}/{self._service(address)}/inbox", params={"payload": payload}
        )
        rows = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and isinstance(r.get("mid"), str) and r["mid"] for r in rows
        ):
            raise SchemaDrift(self.name, expected="'messages' rows with 'mid'", got=data)
        return [
            MessageRow(
                id=r["mid"],
                sender=(r.get("textFrom") or "").strip(),
                subject=r.get("textSubject") or "",
                received_at=_at(r.get("textDate")),
            )
            for r in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        # The body answer carries no headers, so they come from the listing,
        # which costs no solve and spares one for an id that is already gone.
        row = next((r for r in await self.list(address) if r.id == id), None)
        if row is None:
            raise MessageGone(f"smailpro: message {id} is gone")
        resp = await self.http.send(
            "GET",
            f"{SITE}/app/message",
            params={"email": address.value, "mid": id},
            headers={"x-captcha": await self._captcha()},
        )
        payload = resp.text.strip()
        if resp.status_code == 403:
            with suppress(ValueError):  # a non-JSON 403 falls through to drift
                _refused_token(json.loads(payload))
        if resp.status_code != 200 or not payload:
            raise SchemaDrift(
                self.name, expected="a signed payload from message", got=resp.text[:200]
            )
        data = await self.http.json(
            "GET", f"{SONJJ}/{self._service(address)}/message", params={"payload": payload}
        )
        error = data.get("detail", {}).get("error", {}) if isinstance(data, dict) else {}
        if isinstance(error, dict) and error.get("message") == "Message not found!":
            raise MessageGone(f"smailpro: message {id} is gone")
        html = data.get("body") if isinstance(data, dict) else None
        if not isinstance(html, str):
            raise SchemaDrift(self.name, expected="a 'body' from message", got=data)
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
