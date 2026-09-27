"""cloudflare_temp_email (github.com/dreamhunter2333/cloudflare_temp_email, MIT).

An open-source temp-mail worker that anyone can deploy on Cloudflare. This
adapter defaults to the author's public instance: the web app at mail.awsl.uk,
the API at temp-email-api.awsl.uk. Another deployment is a subclass that
overrides `api` and `page`; nothing else here is instance-specific, because
the domains, the Turnstile sitekey and the creation switches are read from
`/open_api/settings` on every generate().

Creating an address needs a Turnstile token in `cf_token` when the instance
sets a sitekey (the public one does). It answers with a JWT, which is the only
credential for the inbox: it goes in Address.state and in `Authorization:
Bearer` on every later call. The JWT carries no `exp`; it works until the
address is deleted, which the public instance does after 100 days without
activity. Reading needs no solve.

Every row, in the listing and on its own, carries the raw RFC 822 source in
`raw`, parsed here with the stdlib. `x-lang: en` pins the worker's error text
to English, so a refused token is recognisable by its message.
"""

from __future__ import annotations

import email
import email.policy
import json
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import ClassVar

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

LANG = {"x-lang": "en"}
# i18n/en.ts TurnstileCheckFailedMsg, answered with HTTP 400.
REFUSED_TOKEN = "Human verification check failed"
PAGE_SIZE = 100  # the worker rejects a larger limit


def _at(value: object) -> datetime | None:
    # created_at is SQLite's CURRENT_TIMESTAMP: "YYYY-MM-DD HH:MM:SS" in UTC.
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse(raw: str) -> email.message.EmailMessage:
    return email.message_from_string(raw, policy=email.policy.default)


def _body(parsed: email.message.EmailMessage, subtype: str) -> str:
    part = parsed.get_body(preferencelist=(subtype,))
    if part is None:
        return ""
    try:
        return part.get_content()
    except LookupError as exc:
        raise SchemaDrift(
            "cftempmail",
            expected="a body in a charset Python knows",
            got=part.get_content_charset(),
        ) from exc


def _row(row: object) -> dict:
    if not (
        isinstance(row, dict) and isinstance(row.get("id"), int) and isinstance(row.get("raw"), str)
    ):
        raise SchemaDrift("cftempmail", expected="a mail row with int 'id' and str 'raw'", got=row)
    return row


class CfTempMail(Provider):
    name = "cftempmail"
    api: ClassVar[str] = "https://temp-email-api.awsl.uk"
    page: ClassVar[str] = "https://mail.awsl.uk/"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("mail.awsl.uk",),
        domains=(),  # read from /open_api/settings on each generate()
        domain_count=3,
        # Deleted after 100 days without activity, per the instance's notice.
        address_ttl=None,
        message_ttl=timedelta(days=100),
        push=False,
        delete=True,
        # The worker prepends the instance's prefix ("tmp") to any chosen name.
        custom_local=False,
        self_hosted=True,
        needs_residential_ip=False,
        needs_state=True,
        needs_solver=True,
        max_concurrency=2,
    )

    def _auth(self, address: Address) -> dict[str, str]:
        if not address.state:
            raise NotSupported(f"{self.name} needs the JWT issued by generate()")
        return {**LANG, "Authorization": f"Bearer {address.state}"}

    async def _settings(self) -> dict:
        data = await self.http.json("GET", f"{self.api}/open_api/settings", headers=LANG)
        domains = data.get("domains") if isinstance(data, dict) else None
        if not (isinstance(domains, list) and domains and all(isinstance(d, str) for d in domains)):
            raise SchemaDrift(self.name, expected="'domains' from settings", got=data)
        if data.get("needAuth") or not data.get("enableUserCreateEmail", True):
            raise NotSupported(
                f"{self.name}: this instance does not let strangers create addresses"
            )
        if data.get("disableAnonymousUserCreateEmail"):
            raise NotSupported(f"{self.name}: this instance only creates addresses for its users")
        return data

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.local:
            raise NotSupported(f"{self.name} prefixes every name, so it cannot honour a local part")
        if opts.kind is not None and opts.kind is not Kind.OWN_DOMAIN:
            raise NotSupported(f"{self.name} does not serve {opts.kind}")
        settings = await self._settings()
        if opts.domain and opts.domain not in settings["domains"]:
            raise NotSupported(f"{self.name} does not serve {opts.domain}")
        body = {"domain": opts.domain or secrets.choice(settings["domains"])}
        sitekey = settings.get("cfTurnstileSiteKey")
        if sitekey:
            if self.http.solver is None:
                raise NotSupported(f"{self.name} needs COWBIRD_SOLVER_URL")
            body["cf_token"] = await self.http.solver.turnstile(self.page, sitekey)
        resp = await self.http.send("POST", f"{self.api}/api/new_address", json=body, headers=LANG)
        if resp.status_code == 400 and REFUSED_TOKEN in resp.text:
            # The solver's fault, not a change in the API: must not quarantine us.
            raise SolverUnavailable(f"{self.name} refused the Turnstile token")
        data = self._json(resp)
        if not (
            isinstance(data, dict)
            and isinstance(data.get("address"), str)
            and "@" in data["address"]
            and isinstance(data.get("jwt"), str)
            and data["jwt"]
        ):
            raise SchemaDrift(self.name, expected="'address' and 'jwt' from new_address", got=data)
        return Address(value=data["address"], provider=self.name, state=data["jwt"])

    async def list(self, address: Address) -> list[MessageRow]:
        resp = await self.http.send(
            "GET",
            f"{self.api}/api/mails",
            params={"limit": PAGE_SIZE, "offset": 0},
            headers=self._auth(address),
        )
        if resp.status_code == 401:
            raise AddressExpired(f"{self.name} no longer knows {address.value}")
        data = self._json(resp)
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="'results' from mails", got=data)
        out = []
        for row in map(_row, rows):
            parsed = _parse(row["raw"])
            out.append(
                MessageRow(
                    id=str(row["id"]),
                    sender=str(parsed["From"] or row.get("source") or ""),
                    subject=str(parsed["Subject"] or ""),
                    received_at=_at(row.get("created_at")),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        resp = await self.http.send("GET", f"{self.api}/api/mail/{id}", headers=self._auth(address))
        if resp.status_code == 401:
            raise AddressExpired(f"{self.name} no longer knows {address.value}")
        data = self._json(resp)
        if data is None:  # the worker's answer for an id it does not have
            raise MessageGone(f"{self.name}: message {id} is gone")
        row = _row(data)
        parsed = _parse(row["raw"])
        html = _body(parsed, "html")
        text = _body(parsed, "plain") or html_to_text(html)
        return Message(
            id=str(row["id"]),
            sender=str(parsed["From"] or row.get("source") or ""),
            subject=str(parsed["Subject"] or ""),
            received_at=_at(row.get("created_at")),
            html=html,
            text=text,
            links=extract_links(html or text),
        )

    async def delete(self, address: Address, id: str) -> None:
        resp = await self.http.send(
            "DELETE", f"{self.api}/api/mails/{id}", headers=self._auth(address)
        )
        if resp.status_code == 403:
            raise NotSupported(f"{self.name}: this instance does not let users delete mail")
        data = self._json(resp)
        if not (isinstance(data, dict) and "success" in data):
            raise SchemaDrift(self.name, expected="'success' from delete", got=data)

    def _json(self, resp) -> object:
        if resp.status_code == 200:
            with suppress(ValueError):
                return json.loads(resp.text)
        raise SchemaDrift(
            self.name, expected="a JSON 200", got=f"HTTP {resp.status_code}: {resp.text[:200]}"
        )
