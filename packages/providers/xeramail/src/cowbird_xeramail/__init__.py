"""xeramail.com — own-domain inboxes, behind a Cloudflare Turnstile.

Only creating an address needs a token: `turnstileToken` in the JSON body of
/api/emails/generate, one solve per address. The answer carries a `secret`,
and every later call is authorised by it alone in `X-Email-Secret`, so the
secret is the whole of Address.state. A secret the site no longer accepts
(or an address it has dropped) answers 403 "Invalid email address or secret".

There is no message endpoint to GET (405): the inbox listing already carries
each message's `bodyHtml` and `bodyText`, so get() reads it from there.

Free tier: xeramail.com and phuturemail.com, a chosen local part is allowed,
and addresses live at most 1440 minutes. emailfutureme.com is paid.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

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

SITE = "https://xeramail.com"
PAGE = f"{SITE}/"
SITEKEY = "0x4AAAAAACkhKnNaaUhg71Sk"
DOMAINS = ("xeramail.com", "phuturemail.com")
EXPIRY_MINUTES = 1440  # the free tier's longest


def _body(resp) -> object:
    try:
        return json.loads(resp.text)
    except ValueError:
        return resp.text[:200]


def _at(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except ValueError:
        return None


class Xeramail(Provider):
    name = "xeramail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("xeramail.com",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        address_ttl=timedelta(minutes=EXPIRY_MINUTES),
        message_ttl=timedelta(days=1),
        push=False,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        needs_solver=True,
        max_concurrency=2,
    )

    async def _check_local(self, local: str, domain: str) -> None:
        resp = await self.http.send(
            "POST",
            f"{SITE}/api/emails/check-availability",
            json={"localPart": local, "domain": domain},
        )
        data = _body(resp)
        if not isinstance(data, dict) or not isinstance(data.get("available"), bool):
            raise SchemaDrift(self.name, expected="'available' from check-availability", got=data)
        if not data["available"]:
            raise NotSupported(f"{self.name}: {local}@{domain}: {data.get('error')}")

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.kind not in (None, Kind.OWN_DOMAIN):
            raise NotSupported(f"{self.name} does not serve {opts.kind}")
        domain = opts.domain or DOMAINS[0]
        if domain not in DOMAINS:
            raise NotSupported(f"{self.name} does not serve {domain} on its free tier")
        body: dict[str, object] = {"domain": domain, "expirationMinutes": EXPIRY_MINUTES}
        if opts.local:
            await self._check_local(opts.local, domain)
            body["localPart"] = opts.local
        if self.http.solver is None:
            raise NotSupported(f"{self.name} needs COWBIRD_SOLVER_URL")
        body["turnstileToken"] = await self.http.solver.turnstile(PAGE, SITEKEY)

        resp = await self.http.send("POST", f"{SITE}/api/emails/generate", json=body)
        data = _body(resp)
        error = str(data.get("error", "")) if isinstance(data, dict) else ""
        if resp.status_code == 403 and "captcha" in error.lower():
            # A refused token is the solver's fault, not a change in the API.
            raise SolverUnavailable(f"{self.name} refused the Turnstile token: {error}")
        email = data.get("email") if isinstance(data, dict) else None
        if not (
            resp.status_code < 300
            and isinstance(email, dict)
            and isinstance(email.get("address"), str)
            and "@" in email["address"]
            and isinstance(email.get("secret"), str)
            and email["secret"]
        ):
            raise SchemaDrift(self.name, expected="'email.address' and 'email.secret'", got=data)
        return Address(
            value=email["address"],
            provider=self.name,
            expires_at=_at(email.get("expiresAt")),
            state=email["secret"],
        )

    def _secret(self, address: Address) -> dict[str, str]:
        if not address.state:
            raise NotSupported(f"{self.name} needs the secret issued by generate()")
        return {"X-Email-Secret": address.state}

    async def _messages(self, address: Address) -> list[dict]:
        resp = await self.http.send(
            "GET",
            f"{SITE}/api/emails/{quote(address.value, safe='')}",
            headers=self._secret(address),
        )
        data = _body(resp)
        if resp.status_code == 403:
            raise AddressExpired(f"{self.name} refused the secret for {address.value}")
        email = data.get("email") if isinstance(data, dict) else None
        rows = email.get("messages") if isinstance(email, dict) else None
        if (
            resp.status_code != 200
            or not isinstance(rows, list)
            or not all(isinstance(r, dict) and isinstance(r.get("id"), str) for r in rows)
        ):
            raise SchemaDrift(self.name, expected="'email.messages' rows with 'id'", got=data)
        return rows

    @staticmethod
    def _row(r: dict) -> MessageRow:
        return MessageRow(
            id=r["id"],
            sender=r.get("fromAddress") or "",
            subject=r.get("subject") or "",
            received_at=_at(r.get("receivedAt")),
        )

    async def list(self, address: Address) -> list[MessageRow]:
        return [self._row(r) for r in await self._messages(address)]

    async def get(self, address: Address, id: str) -> Message:
        raw = next((r for r in await self._messages(address) if r["id"] == id), None)
        if raw is None:
            raise MessageGone(f"{self.name}: message {id} is gone")
        row = self._row(raw)
        html = raw.get("bodyHtml") or ""
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html) if html else raw.get("bodyText") or "",
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        resp = await self.http.send(
            "DELETE", f"{SITE}/api/messages/{quote(id, safe='')}", headers=self._secret(address)
        )
        if resp.status_code == 404:
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code >= 300:
            raise SchemaDrift(self.name, expected="2xx from delete", got=_body(resp))
