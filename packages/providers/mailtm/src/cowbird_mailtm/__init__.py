"""mail.tm provider.

mail.tm content-negotiates: with a browser Accept header (what curl_cffi's
impersonation sends by default) it serves XML. curl's default Accept (`*/*`)
gets a JSON-LD envelope with `hydra:member`. Neither is what this adapter
receives, because `Transport.json()` pins `Accept: application/json` for
every provider. Under that header, mail.tm's list endpoints (`/domains`,
`/messages`) return a **plain JSON array**, not an envelope — there is no
`hydra:member` anywhere in what this adapter ever sees. Fixtures here were
recorded through a real `Transport`, not curl, for exactly this reason: a
fixture recorded by a different client than the runtime uses is fiction.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from cowbird.errors import NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://api.mail.tm"


def _pack(address: str, password: str, token: str) -> str:
    """mail.tm's JWT expires, and re-minting it needs the credentials that
    created the account. All three travel together in Address.state, which core
    treats as an opaque string."""
    return json.dumps({"address": address, "password": password, "token": token})


def _unpack(state: str | None) -> dict[str, str]:
    if not state:
        raise NotSupported("mailtm needs the state issued by generate()")
    return json.loads(state)


def _list(payload: object, where: str) -> list:
    """With Accept: application/json (what Transport.json() always sends),
    mail.tm's list endpoints return a plain array — no hydra:member envelope.
    A non-list response means the shape moved."""
    if not isinstance(payload, list):
        got = type(payload).__name__
        raise SchemaDrift("mailtm", expected=f"a JSON array from {where}", got=got)
    return payload


def _field(row: dict, key: str, where: str) -> str:
    """A missing required field is schema drift, not a bare KeyError: this
    names the field mail.tm dropped or renamed and what the row actually had,
    so the quarantine issue is diagnosable instead of a raw stack trace."""
    if key not in row:
        raise SchemaDrift("mailtm", expected=f"{key!r} in {where}", got=list(row))
    return row[key]


def _at(row: dict) -> datetime | None:
    raw = row.get("createdAt")
    return datetime.fromisoformat(raw) if raw else None


class MailTm(Provider):
    name = "mailtm"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("mail.tm",),
        domains=(),  # discovered per call; mail.tm rotates them
        domain_count=1,
        address_ttl=None,
        message_ttl=timedelta(days=7),
        push=False,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        domains_payload = await self.http.json("GET", f"{API}/domains")
        domains = [d["domain"] for d in _list(domains_payload, "domains")]
        if opts.domain and opts.domain not in domains:
            # A caller asking for a domain this backend does not serve is a caller
            # error, not upstream drift. Raising SchemaDrift here would quarantine
            # a perfectly healthy provider because someone passed a typo.
            raise NotSupported(f"mailtm does not serve {opts.domain}; has {domains}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        value = f"{local}@{opts.domain or domains[0]}"
        password = secrets.token_urlsafe(16)

        await self.http.json(
            "POST", f"{API}/accounts", json={"address": value, "password": password}
        )
        auth = await self.http.json(
            "POST", f"{API}/token", json={"address": value, "password": password}
        )
        if "token" not in auth:
            raise SchemaDrift("mailtm", expected="token in /token", got=list(auth))
        state = _pack(value, password, auth["token"])
        return Address(value=value, provider=self.name, state=state)

    def _auth(self, address: Address) -> dict[str, str]:
        return {"Authorization": f"Bearer {_unpack(address.state)['token']}"}

    async def list(self, address: Address) -> list[MessageRow]:
        payload = await self.http.json("GET", f"{API}/messages", headers=self._auth(address))
        rows = _list(payload, "messages")
        return [
            MessageRow(
                id=_field(row, "id", "a message row"),
                sender=row.get("from", {}).get("address", ""),
                subject=row.get("subject", ""),
                received_at=_at(row),
            )
            for row in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        row = await self.http.json("GET", f"{API}/messages/{id}", headers=self._auth(address))
        html = "".join(row.get("html") or [])
        return Message(
            id=_field(row, "id", "a message document"),
            sender=row.get("from", {}).get("address", ""),
            subject=row.get("subject", ""),
            received_at=_at(row),
            html=html,
            text=row.get("text") or html_to_text(html),
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        # A successful delete is HTTP 204 with an empty body (confirmed live).
        # self.http.json() would try json.loads("") and raise SchemaDrift on
        # the success path, so use send() to see the status instead.
        resp = await self.http.send(
            "DELETE", f"{API}/messages/{id}", headers=self._auth(address)
        )
        if resp.status_code < 300:
            return
        if resp.status_code == 404:
            # Deleting an already-deleted (or never-existed) message is not a
            # caller error: the caller wanted the message gone, and it is
            # gone. Deleting twice must not raise. Deliberate idempotency,
            # not a missed case.
            return
        # 401/403 means wrong or expired credentials, not a locked/missing
        # message; any other 4xx is unexpected. Both are ProviderDown
        # (reroutable) rather than surfaced to the caller as though the
        # message itself were the problem.
        raise ProviderDown(f"mailtm: delete failed, HTTP {resp.status_code}")
