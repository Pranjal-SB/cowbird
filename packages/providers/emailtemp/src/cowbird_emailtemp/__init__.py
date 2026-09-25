"""emailtemp.org and emailgenerator.org: one Laravel "TMail" script, two hosts.

The mailbox is the encrypted `email` cookie. GET /en gives a CSRF token and a
Laravel session; POST /messages with both mints a mailbox (setting `email`)
when there is none, and lists it when there is. A fresh session given only
the `email` cookie reads the same mailbox, so every request runs on a
throwaway session and the host plus that cookie travel in Address.state. An
`email` cookie the site cannot read silently gets a brand-new mailbox, which
is why list() checks the mailbox it was answered for.

Rows carry the HTML body (`content`) and a UTC `receivedAt`, so get() reads
the listing and never touches /view.

POST /create (name, domain) switches the session to a chosen address, but
only for a session that already holds a mailbox. A taken address is refused
with a redirect back to /change and no new cookie; the inbox stays private.
"""

from __future__ import annotations

import json
import random
import re
import string
from datetime import UTC, datetime
from typing import NamedTuple

from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider


class _Site(NamedTuple):
    base: str
    session: str
    domains: tuple[str, ...]


SITES = {
    "emailtemp.org": _Site("https://emailtemp.org", "emailtemp_session", ("tormails.com",)),
    # Bare emailgenerator.org 301s to www, which turns a POST into a GET.
    "emailgenerator.org": _Site(
        "https://www.emailgenerator.org",
        "emailgenerator_session",
        (
            "uxmil.com",
            "pluniversity.edu.pl",
            "ehost.cam",
            "grnail.cam",
            "qmail.host",
            "gmaily.cc",
        ),
    ),
}
_XHR = {"X-Requested-With": "XMLHttpRequest"}
_CSRF = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
_WHEN = "%Y-%m-%d %H:%M:%S"


def _pick(domain: str | None) -> str:
    if domain is None:
        return random.choice(tuple(SITES))
    for site, spec in SITES.items():
        if domain in spec.domains:
            return site
    raise NotSupported(f"emailtemp has no host serving {domain}")


def _local() -> str:
    # The site's own shape: seven letters and three digits.
    return "".join(random.choices(string.ascii_lowercase, k=7)) + f"{random.randrange(1000):03d}"


class EmailTemp(Provider):
    name = "emailtemp"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=tuple(SITES),
        domains=tuple(d for s in SITES.values() for d in s.domains),
        domain_count=sum(len(s.domains) for s in SITES.values()),
        # The `email` cookie's Max-Age is 8h, but that only binds a browser;
        # the site's own retention is unpublished.
        address_ttl=None,
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        # The state is the inbox key: whoever holds the cookie reads the mail.
        needs_state=True,
        max_concurrency=1,
        fresh_session=True,
    )

    def _inbox(self, address: Address) -> tuple[str, str]:
        """The host and the `email` cookie from Address.state."""
        try:
            state = json.loads(address.state or "")
            site, cookie = state["site"], state["cookie"]
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported(
                "emailtemp needs the host and mailbox cookie from generate()"
            ) from exc
        if site not in SITES:
            raise NotSupported(f"emailtemp does not serve {site}")
        return site, cookie

    async def _home(self, site: str) -> tuple[str, dict[str, str]]:
        """A CSRF token and the Laravel session cookie it belongs to."""
        spec = SITES[site]
        home = await self.http.send("GET", f"{spec.base}/en")
        csrf, session = _CSRF.search(home.text), home.cookies.get(spec.session)
        if not csrf or not session:
            raise SchemaDrift(
                self.name,
                expected=f"a csrf-token meta tag and {spec.session} from /en",
                got=home.text[:200],
            )
        return csrf.group(1), {spec.session: session}

    async def _messages(self, site: str, csrf: str, cookies: dict[str, str]):
        """POST /messages: the response and its validated {mailbox, messages}."""
        resp = await self.http.send(
            "POST",
            f"{SITES[site].base}/messages",
            data={"_token": csrf, "captcha": ""},
            headers=_XHR,
            cookies=cookies,
        )
        try:
            data = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        if not (
            isinstance(data, dict)
            and isinstance(data.get("mailbox"), str)
            and isinstance(data.get("messages"), list)
            and all(isinstance(r, dict) and "id" in r for r in data["messages"])
        ):
            raise SchemaDrift(
                self.name, expected="a 'mailbox' and a list of 'messages' with 'id'", got=data
            )
        return resp, data

    def _row(self, r: dict) -> MessageRow:
        when = r.get("receivedAt")
        try:
            received = None if when is None else datetime.strptime(when, _WHEN).replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise SchemaDrift(self.name, expected=f"receivedAt as {_WHEN}", got=when) from exc
        name, email = r.get("from") or "", r.get("from_email") or ""
        return MessageRow(
            id=str(r["id"]),
            sender=f"{name} <{email}>" if name and email else name or email,
            subject=r.get("subject") or "",
            received_at=received,
        )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        site = _pick(opts.domain)
        csrf, session = await self._home(site)
        resp, data = await self._messages(site, csrf, session)
        inbox, value = resp.cookies.get("email"), data["mailbox"]
        if not inbox:
            raise SchemaDrift(self.name, expected="an email cookie from /messages", got=data)
        domain = opts.domain or value.rpartition("@")[2]
        if opts.local or domain != value.rpartition("@")[2]:
            local = opts.local or _local()
            created = await self.http.send(
                "POST",
                f"{SITES[site].base}/create",
                data={"_token": csrf, "name": local, "domain": domain},
                cookies={**session, "email": inbox},
                # The new cookie is on the 302 itself; the page it leads to has none.
                allow_redirects=False,
            )
            inbox, value = created.cookies.get("email"), f"{local}@{domain}"
            if not inbox:
                raise NotSupported(f"emailtemp: {value} is taken")
        return Address(
            value=value,
            provider=self.name,
            state=json.dumps({"site": site, "cookie": inbox}, separators=(",", ":")),
        )

    async def _read(self, address: Address) -> list[dict]:
        site, cookie = self._inbox(address)
        csrf, session = await self._home(site)
        _, data = await self._messages(site, csrf, {**session, "email": cookie})
        if data["mailbox"].lower() != address.value.lower():
            raise AddressExpired(f"emailtemp: {address.value} is gone")
        return data["messages"]

    async def list(self, address: Address) -> list[MessageRow]:
        return [self._row(r) for r in await self._read(address)]

    async def get(self, address: Address, id: str) -> Message:
        raw = next((r for r in await self._read(address) if str(r["id"]) == id), None)
        if raw is None:
            raise MessageGone(f"emailtemp: message {id} is gone")
        html = raw.get("content")
        if not isinstance(html, str):
            raise SchemaDrift(self.name, expected="an HTML 'content' on the row", got=raw)
        row = self._row(raw)
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
