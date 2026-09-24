"""mail.re146.dev — open REST API, described at /api/swagger-json.

Any local part at a served domain receives, so generate() only fetches the
domain list. The inbox is keyed by md5 of the address, not the address itself,
and a message is served as its raw RFC 822 source.

The domains churn (several are throwaway registrations), so they are fetched,
never hardcoded.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://mail.re146.dev"


def _key(address: Address) -> str:
    return hashlib.md5(address.value.lower().encode()).hexdigest()


def _at(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _body(parsed: email.message.EmailMessage, subtype: str) -> str:
    part = parsed.get_body(preferencelist=(subtype,))
    if part is None:
        return ""
    try:
        return part.get_content()
    except LookupError as exc:
        raise SchemaDrift(
            "re146", expected="a body in a charset Python knows", got=part.get_content_charset()
        ) from exc


class Re146(Provider):
    name = "re146"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("mail.re146.dev",),
        domains=(),
        domain_count=13,
        # From the site's own listing; not measured here.
        address_ttl=timedelta(days=1),
        message_ttl=timedelta(hours=1),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        domains = await self.http.json("GET", f"{SITE}/api/domains")
        if not isinstance(domains, list) or not domains or not all(
            isinstance(d, str) for d in domains
        ):
            raise SchemaDrift(self.name, expected="a list of domain names", got=domains)
        if opts.domain and opts.domain not in domains:
            raise NotSupported(f"re146 does not serve {opts.domain}")
        domain = opts.domain or secrets.choice(domains)
        local = opts.local or f"cb{secrets.token_hex(5)}"
        return Address(value=f"{local}@{domain}", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        rows = await self.http.json("GET", f"{SITE}/api/messages/{_key(address)}")
        if not isinstance(rows, list) or not all(isinstance(r, dict) and "id" in r for r in rows):
            raise SchemaDrift(self.name, expected="an array of rows with 'id'", got=rows)
        return [
            MessageRow(
                id=row["id"],
                sender=row.get("from") or "",
                subject=row.get("subject") or "",
                received_at=_at(row.get("receivedAt")),
            )
            for row in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        resp = await self.http.send("GET", f"{SITE}/storage/{_key(address)}/{id}")
        if resp.status_code == 404:
            raise MessageGone(f"re146: message {id} is gone")
        if resp.status_code >= 400:
            raise ProviderDown(f"re146: storage answered HTTP {resp.status_code}")
        parsed = email.message_from_string(resp.text, policy=email.policy.default)
        html = _body(parsed, "html")
        text = _body(parsed, "plain") or html_to_text(html)
        try:
            received = parsedate_to_datetime(parsed["Date"]).astimezone(UTC)
        except (TypeError, ValueError):
            received = None
        return Message(
            id=id,
            sender=str(parsed["From"] or ""),
            subject=str(parsed["Subject"] or ""),
            received_at=received,
            html=html,
            text=text,
            links=extract_links(html or text),
        )
