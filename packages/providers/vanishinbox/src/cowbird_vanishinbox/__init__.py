"""vanishinbox.com — nine domains, three of them .edu.pl, behind Cloudflare Turnstile.

The page makes addresses up in the browser: the server keeps no record of
them and receives mail for any local part on its domains. So generate() picks
the address itself, with a random hex local part rather than the site's
adjective-animal-number, because any verified visitor can read any inbox by
name and the site's names are guessable.

Reading needs a pass. A Turnstile token POSTed to /api/verify-turnstile earns
an HttpOnly `vi_hv` cookie, `<unix expiry>.<hmac>`, good for two hours for
every address; a token is single-use. Without the cookie the inbox answers
`403 {"error": "verification_required"}`. So there is one solve per two-hour
window per process, not one per address. The cookie travels in Address.state
(treat it as a secret) so another process can read without solving; state is
optional, since list() earns a pass itself when it has none.

The listing carries whole messages, body included; there is no per-message
endpoint, so a message the site has dropped is one no longer listed. Deleting
on the page only hides a message in the browser.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from cowbird.errors import MessageGone, NotSupported, SchemaDrift, SolverUnavailable
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://vanishinbox.com"
PAGE = f"{SITE}/"
SITEKEY = "0x4AAAAAADyGrnNGqa-_Rg6x"
COOKIE = "vi_hv"
OWN_DOMAINS = (
    "myerly.com",
    "fommie.com",
    "whoopza.org",
    "fommie.online",
    "fommie.store",
    "whoopza.store",
)
EDU_DOMAINS = ("reason.edu.pl", "realize.edu.pl", "remind.edu.pl")
KINDS = {Kind.OWN_DOMAIN: OWN_DOMAINS, Kind.EDU: EDU_DOMAINS}
LOCAL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# A pass this close to its expiry is renewed rather than sent.
MARGIN = 60


def _live(cookie: object) -> bool:
    expiry, _, _ = str(cookie).partition(".")
    return expiry.isdigit() and int(expiry) > time.time() + MARGIN


def _from_state(address: Address) -> str | None:
    try:
        cookie = json.loads(address.state or "")[COOKIE]
    except (ValueError, TypeError, KeyError):
        return None
    return cookie if isinstance(cookie, str) and _live(cookie) else None


def _at(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _address(opts: GenerateOptions) -> str:
    if opts.kind is not None and opts.kind not in KINDS:
        raise NotSupported(f"vanishinbox does not serve {opts.kind}")
    allowed = KINDS[opts.kind] if opts.kind else OWN_DOMAINS + EDU_DOMAINS
    domain = opts.domain or secrets.choice(KINDS[opts.kind or Kind.OWN_DOMAIN])
    if domain not in allowed:
        raise NotSupported(f"vanishinbox does not serve {domain} for {opts.kind or 'any kind'}")
    local = opts.local if opts.local is not None else secrets.token_hex(6)
    if not LOCAL.fullmatch(local):
        raise NotSupported(f"vanishinbox cannot use the local part {local!r}")
    return f"{local}@{domain}"


class VanishInbox(Provider):
    name = "vanishinbox"
    caps = Capabilities(
        kind=frozenset(KINDS),
        sites=("vanishinbox.com",),
        domains=OWN_DOMAINS + EDU_DOMAINS,
        domain_count=len(OWN_DOMAINS + EDU_DOMAINS),
        # Any local part receives, so the address itself never lapses. The site
        # says mail is deleted after ten minutes; not measured.
        address_ttl=None,
        message_ttl=timedelta(minutes=10),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_solver=True,
        poll_interval=5.0,
    )

    def __init__(self, http) -> None:
        super().__init__(http)
        self._pass: str | None = None
        self._lock = asyncio.Lock()

    async def _verify(self, stale: str | None = None) -> str:
        """A live pass, earning one only if none is held (or the held one is `stale`)."""
        async with self._lock:
            if self._pass and self._pass != stale and _live(self._pass):
                return self._pass
            if self.http.solver is None:
                raise NotSupported(f"{self.name} needs COWBIRD_SOLVER_URL")
            token = await self.http.solver.turnstile(PAGE, SITEKEY)
            resp = await self.http.send(
                "POST", f"{SITE}/api/verify-turnstile", json={"token": token}
            )
            try:
                data = json.loads(resp.text)
            except ValueError:
                data = resp.text[:200]
            if isinstance(data, dict) and data.get("success") is False:
                # A token the site will not accept is the solver's fault.
                raise SolverUnavailable(f"vanishinbox refused the Turnstile token: {data}")
            cookie = resp.cookies.get(COOKIE)
            if not (isinstance(data, dict) and data.get("success") is True and cookie):
                raise SchemaDrift(
                    self.name, expected=f"success and a {COOKIE} cookie from verify", got=data
                )
            self._pass = cookie
            return cookie

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        value = _address(opts or GenerateOptions())
        cookie = await self._verify()
        return Address(value, self.name, state=json.dumps({COOKIE: cookie}, separators=(",", ":")))

    async def _inbox(self, address: Address) -> list[dict]:
        url = f"{SITE}/api/inbox/{quote(address.value, safe='')}"
        held = self._pass if self._pass and _live(self._pass) else None
        cookie = held or _from_state(address) or await self._verify()
        resp = await self.http.send("GET", url, cookies={COOKIE: cookie})
        if resp.status_code == 403:
            # Revoked or rejected early: earn a fresh pass once and retry.
            cookie = await self._verify(stale=cookie)
            resp = await self.http.send("GET", url, cookies={COOKIE: cookie})
        try:
            data = json.loads(resp.text)
        except ValueError:
            data = resp.text[:200]
        emails = data.get("emails") if isinstance(data, dict) else None
        if (
            resp.status_code != 200
            or not isinstance(emails, list)
            or not all(isinstance(e, dict) and isinstance(e.get("id"), str) for e in emails)
        ):
            raise SchemaDrift(self.name, expected="'emails' rows with 'id' from inbox", got=data)
        return emails

    async def list(self, address: Address) -> list[MessageRow]:
        return [
            MessageRow(
                id=e["id"],
                sender=str(e.get("from") or ""),
                subject=str(e.get("subject") or ""),
                received_at=_at(e.get("receivedAt")),
            )
            for e in await self._inbox(address)
        ]

    async def get(self, address: Address, id: str) -> Message:
        email = next((e for e in await self._inbox(address) if e["id"] == id), None)
        if email is None:
            raise MessageGone(f"vanishinbox: message {id} is gone")
        html = email.get("body")
        if not isinstance(html, str):
            raise SchemaDrift(self.name, expected="a string 'body' in the inbox row", got=email)
        text = email.get("bodyText")
        return Message(
            id=id,
            sender=str(email.get("from") or ""),
            subject=str(email.get("subject") or ""),
            received_at=_at(email.get("receivedAt")),
            html=html,
            text=text if isinstance(text, str) and text else html_to_text(html),
            links=extract_links(html),
        )
