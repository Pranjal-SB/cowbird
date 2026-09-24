"""disposablemail.com, fakemail.net and minuteinbox.com: one engine, three front doors.

The inbox is a cookie naming the address (TMA on the first two, MI on
minuteinbox). generate() loads the home page for its CSRF token and PHP
session, then asks /index/index for an address with both; the answer sets the
inbox cookie. On a shared jar that cookie makes a second generate() hand back
the first address, so every request runs on a fresh session and the host plus
the cookie travel in Address.state.

/index/refresh lists the inbox and /email/id/<id> serves the decoded HTML body.
Both work from a fresh session given only the inbox cookie; /index/email,
which carries headers too, answers `false` there, so get() takes sender and
subject from the listing row. Rows carry only a relative "13 sec. ago", so
received_at is None.

fakemail and minuteinbox prefix every body with a UTF-8 BOM, which json.loads
refuses, so bodies are read as text and stripped.
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider


class _Site(NamedTuple):
    cookie: str
    # Each host handed out one domain across every session seen in recon.
    domain: str
    # The life /index/zivot reports for a new inbox.
    # ponytail: /expirace/<secs> extends it up to 14 days; call it from
    # generate() once a caller needs an inbox to outlive this.
    life: timedelta


SITES = {
    "disposablemail.com": _Site("TMA", "dropoffs.org", timedelta(hours=1)),
    "fakemail.net": _Site("TMA", "forliion.com", timedelta(hours=1)),
    "minuteinbox.com": _Site("MI", "minafter.com", timedelta(minutes=10)),
}
_XHR = {"X-Requested-With": "XMLHttpRequest"}
_CSRF = re.compile(r'const CSRF\s*=\s*"([0-9a-f]+)"')
_BOM = "﻿"
# What /email/id/<id> answers, with a 200, for an id the inbox lacks.
_NO_BODY = "There was en error"


def _base(site: str) -> str:
    return f"https://www.{site}"


def _pick(domain: str | None) -> str:
    if domain is None:
        return random.choice(tuple(SITES))
    for site, spec in SITES.items():
        if spec.domain == domain:
            return site
    raise NotSupported(f"disposablemail has no host serving {domain}")


class DisposableMail(Provider):
    name = "disposablemail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=tuple(SITES),
        domains=tuple(s.domain for s in SITES.values()),
        domain_count=len(SITES),
        # The shortest host's (minuteinbox); expires_at carries each host's own.
        address_ttl=timedelta(minutes=10),
        # Mail lives as long as its inbox.
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        # The state is the inbox key: whoever holds the cookie reads the mail.
        needs_state=True,
        max_concurrency=1,
        fresh_session=True,
    )

    def _json(self, body: str) -> object:
        try:
            return json.loads(body.lstrip(_BOM))
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=body[:200]) from exc

    def _inbox(self, address: Address) -> tuple[str, dict[str, str]]:
        """The host and the inbox cookie from Address.state."""
        try:
            state = json.loads(address.state or "")
            site = state["site"]
            return site, {SITES[site].cookie: state["cookie"]}
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported(
                "disposablemail needs the host and inbox cookie issued by generate()"
            ) from exc

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        if opts and opts.local:
            raise NotSupported("disposablemail picks the local part")
        site = _pick(opts.domain if opts else None)
        spec, base = SITES[site], _base(site)
        home = await self.http.send("GET", f"{base}/")
        csrf, session = _CSRF.search(home.text), home.cookies.get("PHPSESSID")
        if not csrf or not session:
            raise SchemaDrift(
                self.name,
                expected="a CSRF token and PHPSESSID from the home page",
                got=home.text[:200],
            )
        resp = await self.http.send(
            "GET",
            f"{base}/index/index",
            params={"csrf_token": csrf.group(1)},
            headers=_XHR,
            cookies={"PHPSESSID": session},
        )
        data, inbox = self._json(resp.text), resp.cookies.get(spec.cookie)
        if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not inbox:
            raise SchemaDrift(
                self.name, expected=f"an 'email' and a {spec.cookie} cookie", got=data
            )
        return Address(
            value=data["email"],
            provider=self.name,
            expires_at=datetime.now(UTC) + spec.life,
            state=json.dumps({"site": site, "cookie": inbox}, separators=(",", ":")),
        )

    async def list(self, address: Address) -> list[MessageRow]:
        site, cookies = self._inbox(address)
        body = await self.http.text(
            "GET", f"{_base(site)}/index/refresh", headers=_XHR, cookies=cookies
        )
        # An inbox the site no longer knows answers with nothing at all.
        if not body.lstrip(_BOM):
            raise AddressExpired(f"disposablemail: {address.value} is gone")
        rows = self._json(body)
        # The site's own script reads `false` as "no mail".
        if rows is False:
            return []
        if not isinstance(rows, list) or not all(isinstance(r, dict) and "id" in r for r in rows):
            raise SchemaDrift(self.name, expected="a list of rows with 'id'", got=rows)
        return [
            MessageRow(
                id=str(r["id"]),
                sender=r.get("od") or "",
                subject=r.get("predmet") or "",
                received_at=None,
            )
            for r in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        row = next((r for r in await self.list(address) if r.id == id), None)
        if row is None:
            raise MessageGone(f"disposablemail: message {id} is gone")
        site, cookies = self._inbox(address)
        html = (
            await self.http.text("GET", f"{_base(site)}/email/id/{id}", cookies=cookies)
        ).lstrip(_BOM)
        if html.startswith(_NO_BODY):
            raise MessageGone(f"disposablemail: message {id} is gone")
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
