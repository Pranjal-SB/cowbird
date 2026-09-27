"""zenvex.dev — choosable local parts on six domains, behind a Cloudflare Turnstile.

There is no create call: an inbox is any address on a served domain, keyed by
the address alone, so anyone who knows it can read it.

The API only answers what looks like the site's own fetch(): Sec-Fetch-* of a
same-origin request, plus the `zvx_csrf` cookie echoed in X-Zenvex-CSRF. The
home page sets that cookie. The inbox then answers 403 until the session has
passed Turnstile once through POST /turnstile/verify; the pass lives on the
session cookie, so one solve serves every address on the same Transport until
the site drops it, and a 403 then triggers one fresh solve.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import string
from datetime import UTC, datetime
from urllib.parse import quote

from cowbird.errors import MessageGone, NotSupported, SchemaDrift, SolverUnavailable
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://zenvex.dev"
PAGE = f"{SITE}/"
SITEKEY = "0x4AAAAAADCsh7evwKvOsjm5"
DOMAINS = ("encg.edu.pl", "ensam.edu.pl", "ofppt.edu.pl", "souss.dev", "zenvex.edu.pl", "znvx.me")
FETCH = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
# What the site's own input normalises a local part to.
_LOCAL_RE = re.compile(r"[a-z0-9_-][a-z0-9._-]{0,63}")
_LOCAL = string.ascii_lowercase + string.digits


def _is_edu(domain: str) -> bool:
    return domain.endswith(".edu.pl")


def _at(value: object) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if isinstance(value, int | float) else None


def _refused(status: int, body: dict) -> bool:
    """403 ProxyError "Use the Zenvex website" is what a session without a
    Turnstile pass (or without the csrf header) gets from every API route."""
    return status == 403 and "website" in str((body.get("error") or {}).get("message", ""))


class Zenvex(Provider):
    name = "zenvex"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN, Kind.EDU}),
        sites=("zenvex.dev",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        address_ttl=None,
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_solver=True,
        max_concurrency=2,
        poll_interval=5.0,
    )

    def __init__(self, http) -> None:
        super().__init__(http)
        self._csrf: str | None = None
        # Bumped on every pass, so concurrent readers refused by the same
        # lapsed session wait for one solve instead of each paying for one.
        self._pass = 0
        self._lock = asyncio.Lock()

    def _need_solver(self) -> None:
        if self.http.solver is None:
            raise NotSupported(f"{self.name} needs COWBIRD_SOLVER_URL")

    async def _home(self) -> None:
        resp = await self.http.send("GET", PAGE)
        # Only a first visit, or one after the cookie lapsed, re-issues it.
        self._csrf = resp.cookies.get("zvx_csrf") or self._csrf
        if not self._csrf:
            raise SchemaDrift(
                self.name, expected="a zvx_csrf cookie from /", got=sorted(resp.cookies)
            )

    async def _call(self, method: str, path: str, **kw) -> tuple[int, dict]:
        if self._csrf is None:
            await self._home()
        resp = await self.http.send(
            method, f"{SITE}{path}", headers={**FETCH, "X-Zenvex-CSRF": self._csrf}, **kw
        )
        try:
            body = json.loads(resp.text)
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
            raise SchemaDrift(
                self.name, expected=f"a success envelope from {path}", got=resp.text[:200]
            )
        return resp.status_code, body

    async def _domains(self) -> tuple[list[str], str]:
        _, body = await self._call("GET", "/api/domains")
        result = body.get("result")
        domains = result.get("domains") if isinstance(result, dict) else None
        if (
            not isinstance(domains, list)
            or not domains
            or not all(isinstance(d, str) for d in domains)
        ):
            raise SchemaDrift(self.name, expected="'domains' from /api/domains", got=body)
        default = result.get("default")
        return domains, default if default in domains else domains[0]

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        self._need_solver()  # an address nobody can read is no use
        if opts.kind is not None and opts.kind not in self.caps.kind:
            raise NotSupported(f"zenvex does not serve {opts.kind}")
        if opts.local is not None and not _LOCAL_RE.fullmatch(opts.local):
            raise NotSupported(f"zenvex would rewrite the local part {opts.local!r}")
        domains, default = await self._domains()
        if opts.kind is Kind.EDU:
            domains = [d for d in domains if _is_edu(d)]
            default = domains[0] if domains else ""
        elif opts.kind is Kind.OWN_DOMAIN:
            domains = [d for d in domains if not _is_edu(d)]
            default = default if default in domains else (domains[0] if domains else "")
        domain = opts.domain or default
        if domain not in domains:
            raise NotSupported(f"zenvex does not serve {domain or opts.kind} here; has {domains}")
        local = opts.local or "".join(secrets.choice(_LOCAL) for _ in range(10))
        return Address(value=f"{local}@{domain}", provider=self.name)

    async def _verify(self, seen: int) -> None:
        self._need_solver()
        async with self._lock:
            if self._pass != seen:
                return  # another reader already renewed the pass
            if self._pass:
                await self._home()  # renewing: the csrf cookie may have lapsed too
            token = await self.http.solver.turnstile(PAGE, SITEKEY)
            status, body = await self._call("POST", "/turnstile/verify", json={"token": token})
            if body["success"] is not True:
                # A token zenvex will not take is the solver's fault, not drift.
                message = (body.get("error") or {}).get("message")
                if message == "Verification failed":
                    raise SolverUnavailable(f"zenvex refused the Turnstile token: {message}")
                raise SchemaDrift(self.name, expected="success from /turnstile/verify", got=body)
            self._pass += 1

    async def _read(self, path: str, **kw) -> tuple[int, dict]:
        seen = self._pass
        status, body = await self._call("GET", path, **kw)
        if not _refused(status, body):
            return status, body
        await self._verify(seen)
        status, body = await self._call("GET", path, **kw)
        if _refused(status, body):
            raise SchemaDrift(self.name, expected=f"{path} to answer a verified session", got=body)
        return status, body

    async def list(self, address: Address) -> list[MessageRow]:
        _, body = await self._read(
            f"/api/emails/{quote(address.value, safe='')}", params={"limit": "50", "offset": "0"}
        )
        rows = body.get("result")
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] for r in rows
        ):
            raise SchemaDrift(self.name, expected="a list of rows with 'id'", got=body)
        return [
            MessageRow(
                id=r["id"],
                sender=r.get("from_address") or "",
                subject=r.get("subject") or "",
                received_at=_at(r.get("received_at")),
            )
            for r in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        status, body = await self._read(f"/api/inbox/{quote(id, safe='')}")
        if status == 404 and (body.get("error") or {}).get("name") == "NotFound":
            raise MessageGone(f"zenvex: message {id} is gone")
        mail = body.get("result")
        if not (
            status == 200
            and isinstance(mail, dict)
            and ("html_content" in mail or "text_content" in mail)
        ):
            raise SchemaDrift(self.name, expected="a message with html/text_content", got=body)
        html = mail.get("html_content") or ""
        return Message(
            id=id,
            sender=mail.get("from_address") or "",
            subject=mail.get("subject") or "",
            received_at=_at(mail.get("received_at")),
            html=html,
            text=mail.get("text_content") or html_to_text(html),
            links=extract_links(html),
        )
