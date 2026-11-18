"""anonymmail.net provider.

CodeIgniter 4 app behind Cloudflare. Everything is form-encoded POST with a
CSRF token (`csrf_test_name`) fetched from GET /api/csrf before each call. The
inbox is keyed on the *session*, not on the address: the homepage sets three
cookies (am_uid, ci_session, csrf_cookie_name). Without a live session /api/get
answers 200 with an empty body (403 when the csrf cookie is missing too). The
registry runs this provider with fresh_session=True so two callers never share a
jar; the adapter therefore
carries the cookies itself, request to request, and packs them into
Address.state so a separate `cowbird wait` process can read the same inbox.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime

from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links
from cowbird.provider import GenerateOptions, Provider
from cowbird.transport import Transport

# d/m/Y H:i:s, as rendered by the site's own inbox widget.
_DATE = "%d/%m/%Y %H:%M:%S"


def _at(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, _DATE)
    except (TypeError, ValueError):
        return None


def _decode_body(row: dict) -> str:
    """Message bodies arrive base64-encoded, with the `=` padding stripped and
    CRLF line wraps inside; a body that still is not decodable passes through."""
    body = row.get("body") or ""
    clean = "".join(body.split())
    try:
        return base64.b64decode(clean + "=" * (-len(clean) % 4)).decode("utf-8", "replace")
    except ValueError:
        return body


class AnonymMail(Provider):
    name = "anonymmail"
    api = "https://anonymmail.net"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("anonymmail.net",),
        domains=(),  # discovered per call from /api/getDomains
        domain_count=6,
        address_ttl=None,
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        fresh_session=True,
    )

    def __init__(self, http: Transport):
        super().__init__(http)
        # /api/get hands each message out exactly once; this cache is what
        # makes get(id) work after list() has already consumed the row.
        self._cache: dict[str, dict] = {}

    async def _send(self, jar: dict[str, str], method: str, path: str, **kw):
        """One request on the session in `jar`, which the answer's cookies
        update in place: the backend rotates csrf_cookie_name on every call."""
        resp = await self.http.send(method, f"{self.api}{path}", cookies=dict(jar), **kw)
        jar.update(resp.cookies)
        return resp

    def _json(self, resp) -> object:
        try:
            return json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc

    async def _csrf(self, jar: dict[str, str]) -> str:
        """A fresh CSRF token for the next mutating call."""
        payload = self._json(await self._send(jar, "GET", "/api/csrf"))
        if not isinstance(payload, dict) or "hash" not in payload:
            raise SchemaDrift("anonymmail", expected="a csrf hash", got=payload)
        return payload["hash"]

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        jar: dict[str, str] = {}
        # The homepage is what issues the session cookies every call needs.
        await self._send(jar, "GET", "/")
        domain = opts.domain or await self._domain(jar)
        local = opts.local or f"cb{secrets.token_hex(5)}"
        value = f"{local}@{domain}"
        csrf = await self._csrf(jar)
        self._json(
            await self._send(
                jar, "POST", "/api/create", data={"csrf_test_name": csrf, "email": value}
            )
        )
        return Address(value=value, provider=self.name, state=json.dumps({"cookies": jar}))

    async def _domain(self, jar: dict[str, str]) -> str:
        csrf = await self._csrf(jar)
        domains = self._json(
            await self._send(jar, "POST", "/api/getDomains", data={"csrf_test_name": csrf})
        )
        if not isinstance(domains, list) or not domains:
            raise SchemaDrift("anonymmail", expected="a domain list", got=domains)
        for row in domains:
            if not isinstance(row, dict) or "domain" not in row:
                raise SchemaDrift("anonymmail", expected="a domain row", got=row)
        return domains[0]["domain"]

    async def list(self, address: Address) -> list[MessageRow]:
        rows = []
        for row in await self._raw_rows(address):
            # The backend hands each message out exactly once: the next /api/get
            # no longer contains it. Keep the raw row so get() can still serve it.
            self._cache[row["token"]] = row
            rows.append(
                MessageRow(
                    id=row["token"],
                    sender=row.get("from", ""),
                    subject=row.get("subject", ""),
                    received_at=_at(row.get("date")),
                )
            )
        return rows

    async def get(self, address: Address, id: str) -> Message:
        row = self._cache.get(id)
        if row is None:
            await self.list(address)
            row = self._cache.get(id)
        if row is None:
            # Either never existed or was already handed out by an earlier
            # /api/get — from the caller's point of view both are "gone".
            raise MessageGone(f"{self.name}: message {id} is not in the inbox")
        text = _decode_body(row)
        return Message(
            id=id,
            sender=row.get("from", ""),
            subject=row.get("subject", ""),
            received_at=_at(row.get("date")),
            html="",
            text=text,
            links=extract_links(text),
        )

    async def _raw_rows(self, address: Address) -> list[dict]:
        jar = _cookies(address.state)
        csrf = await self._csrf(jar)
        resp = await self._send(
            jar, "POST", "/api/get", data={"csrf_test_name": csrf, "email": address.value}
        )
        if resp.status_code == 403 or not resp.text.strip():
            # The session behind the stored cookies is gone, and so is this inbox:
            # the backend answers a dead session with an empty 200 (or a 403).
            raise AddressExpired(f"{self.name}: session for {address.value} was refused")
        payload = self._json(resp)
        inbox = next(iter(payload.values()), None) if isinstance(payload, dict) else None
        if inbox is None:
            raise SchemaDrift("anonymmail", expected="the inbox object", got=payload)
        return inbox.get("emails") or []


def _cookies(state: str | None) -> dict[str, str]:
    """The session cookies `generate()` packed into Address.state."""
    try:
        cookies = json.loads(state or "")["cookies"]
    except (ValueError, TypeError, KeyError) as exc:
        raise NotSupported("anonymmail needs the state printed by `cowbird new --json`") from exc
    if not isinstance(cookies, dict):
        raise NotSupported("anonymmail state carries no session cookies")
    return {str(k): str(v) for k, v in cookies.items()}
