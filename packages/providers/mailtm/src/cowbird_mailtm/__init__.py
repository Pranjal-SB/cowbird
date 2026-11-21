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
from typing import ClassVar

from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider
from cowbird.transport import Transport

API = "https://api.mail.tm"


def _pack(address: str, password: str, token: str) -> str:
    """mail.tm's JWT expires, and re-minting it needs the credentials that
    created the account. All three travel together in Address.state, which core
    treats as an opaque string."""
    return json.dumps({"address": address, "password": password, "token": token})


def _unpack(state: str | None) -> dict[str, str]:
    try:
        packed = json.loads(state or "")
        return {key: str(packed[key]) for key in ("address", "password", "token")}
    except (ValueError, TypeError, KeyError) as exc:
        raise NotSupported("mailtm needs the state issued by generate()") from exc


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
    api: ClassVar[str] = API
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

    def __init__(self, http: Transport) -> None:
        super().__init__(http)
        # Re-minted tokens by address. Address is frozen, so a fresh JWT cannot
        # go back into its state; later calls in this process take it from here.
        self._tokens: dict[str, str] = {}

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        domains_payload = await self.http.json("GET", f"{self.api}/domains")
        domains = [d["domain"] for d in self._rows(domains_payload, "domains")]
        if opts.domain and opts.domain not in domains:
            # A caller asking for a domain this backend does not serve is a caller
            # error, not upstream drift. Raising SchemaDrift here would quarantine
            # a perfectly healthy provider because someone passed a typo.
            raise NotSupported(f"{self.name} does not serve {opts.domain}; has {domains}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        value = f"{local}@{opts.domain or domains[0]}"
        password = secrets.token_urlsafe(16)

        created = await self.http.send(
            "POST", f"{self.api}/accounts", json={"address": value, "password": password}
        )
        if created.status_code == 422 and opts.local:
            # The caller's chosen name is taken or refused: an answer to them.
            raise NotSupported(f"{self.name} refused the address {value}")
        if created.status_code == 422:
            # Our own random name refused means mail.tm changed its rules.
            raise SchemaDrift(self.name, expected="/accounts to accept a random name",
                              got=created.text[:200])
        if created.status_code >= 400:
            raise ProviderDown(f"{self.name}: account creation failed, HTTP {created.status_code}")
        auth = await self.http.json(
            "POST", f"{self.api}/token", json={"address": value, "password": password}
        )
        if "token" not in auth:
            raise SchemaDrift(self.name, expected="token in /token", got=list(auth))
        state = _pack(value, password, auth["token"])
        return Address(value=value, provider=self.name, state=state)

    def _rows(self, payload: object, where: str) -> list:
        """The rows of a list endpoint. A hook so a mail.tm clone that wraps
        them in an envelope can unwrap it."""
        return _list(payload, where)

    async def _authed(self, method: str, url: str, address: Address):
        """`method url` with the freshest token for `address`. One 401 re-mints
        the token from the packed credentials and retries once; a second 401,
        or a refused re-mint, means the account itself is gone."""
        packed = _unpack(address.state)
        token = self._tokens.get(address.value, packed["token"])
        resp = await self.http.send(method, url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 401:
            return resp
        auth = await self.http.send(
            "POST",
            f"{self.api}/token",
            json={"address": packed["address"], "password": packed["password"]},
        )
        if auth.status_code == 401:
            raise AddressExpired(f"{self.name}: the account for {address.value} is gone")
        minted = self._parse(auth, "/token")
        if not isinstance(minted, dict):
            got = type(minted).__name__
            raise SchemaDrift(self.name, expected="an object from /token", got=got)
        fresh = _field(minted, "token", "/token")
        self._tokens[address.value] = fresh
        resp = await self.http.send(method, url, headers={"Authorization": f"Bearer {fresh}"})
        if resp.status_code == 401:
            raise AddressExpired(f"{self.name}: a fresh token for {address.value} was refused")
        return resp

    def _parse(self, resp, where: str) -> object:
        try:
            return json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(
                self.name, expected=f"a JSON body from {where}", got=resp.text[:200]
            ) from exc

    async def list(self, address: Address) -> list[MessageRow]:
        resp = await self._authed("GET", f"{self.api}/messages", address)
        rows = self._rows(self._parse(resp, "messages"), "messages")
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
        resp = await self._authed("GET", f"{self.api}/messages/{id}", address)
        # An expired or deleted message is a 404 with a JSON error body.
        if resp.status_code == 404:
            raise MessageGone(f"{self.name}: message {id} is gone")
        try:
            row = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(
                self.name, expected="a message document", got=resp.text[:200]
            ) from exc
        if not isinstance(row, dict):
            raise SchemaDrift(self.name, expected="a message document", got=row)
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
        resp = await self._authed("DELETE", f"{self.api}/messages/{id}", address)
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
        raise ProviderDown(f"{self.name}: delete failed, HTTP {resp.status_code}")
