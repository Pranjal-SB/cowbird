"""mtempmail.com, a site running the Lobage temp-mail script.

The inbox is the `email` cookie. POST /get_messages without it mints an inbox
and sets the cookie; with it, it lists that inbox, bodies included, so get()
never needs the /view page. A cookie that no longer names a live inbox gets a
new one back rather than an error, which is why list() checks the mailbox it
was answered with. On a shared jar the cookie from one generate() would be
sent with the next, so every request runs on a fresh session and the cookie
travels in Address.state.

The captcha is off here, and get_messages does not check the CSRF token. /change,
which renames the current inbox to a chosen name and domain, does. So a chosen
address costs a home page load for the token plus a mint to have an inbox to
rename.

zub.edu.pl is an `.edu.pl` domain: a Polish edu name anyone can take, not a
university mailbox.

Inboxes are not private. /change hands anyone the inbox of any name they type,
/view/<id> serves a message with no cookie at all, and every answer lists
the other addresses minted from the same IP.
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

BASE = "https://mtempmail.com"
KINDS = {
    Kind.OWN_DOMAIN: ("mtempmail.com", "fraudfindez.com"),
    Kind.EDU: ("zub.edu.pl",),
}
DOMAINS = tuple(d for domains in KINDS.values() for d in domains)
SESSION = "free_edu_com_temporary_mails_session"
LIFE = timedelta(hours=24)
_XHR = {"X-Requested-With": "XMLHttpRequest"}
_CSRF = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
# receivedAt is the site's wall clock, two hours ahead of the UTC it gives
# history times in.
# ponytail: fixed +02:00, so rows read an hour late if the server follows
# Warsaw onto winter time; derive it from Europe/Warsaw once tzdata ships.
_SITE_TZ = timezone(timedelta(hours=2))


def _allowed(opts: GenerateOptions) -> tuple[str, ...]:
    if opts.kind is not None and opts.kind not in KINDS:
        raise NotSupported(f"mtempmail does not serve {opts.kind}")
    domains = KINDS[opts.kind] if opts.kind else DOMAINS
    if opts.domain is None:
        return domains
    if opts.domain not in domains:
        raise NotSupported(f"mtempmail does not serve {opts.domain} here; has {domains}")
    return (opts.domain,)


def _received(row: dict) -> datetime | None:
    try:
        stamp = datetime.strptime(row["receivedAt"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return None
    return stamp.replace(tzinfo=_SITE_TZ).astimezone(UTC)


class MTempMail(Provider):
    name = "mtempmail"
    caps = Capabilities(
        kind=frozenset(KINDS),
        sites=("mtempmail.com",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        # "Every address and its messages are deleted 24 hours after the
        # address is created."
        address_ttl=LIFE,
        message_ttl=LIFE,
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        max_concurrency=1,
        fresh_session=True,
    )

    def _inbox(self, data: object) -> dict:
        """A get_messages or /change answer, checked."""
        if (
            isinstance(data, dict)
            and isinstance(data.get("mailbox"), str)
            and isinstance(data.get("messages"), list)
            and all(
                isinstance(m, dict)
                and isinstance(m.get("id"), str)
                and isinstance(m.get("content"), str)
                for m in data["messages"]
            )
        ):
            return data
        raise SchemaDrift(
            self.name, expected="a 'mailbox' and 'messages' with 'id' and 'content'", got=data
        )

    def _parse(self, text: str) -> dict:
        try:
            return self._inbox(json.loads(text))
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=text[:200]) from exc

    def _address(self, data: dict, resp: Any) -> Address:
        cookie = resp.cookies.get("email")
        if not cookie:
            raise SchemaDrift(self.name, expected="an 'email' cookie", got=data["mailbox"])
        return Address(
            value=data["mailbox"],
            provider=self.name,
            expires_at=datetime.now(UTC) + LIFE,
            state=json.dumps({"email": cookie}),
        )

    def _cookie(self, address: Address) -> dict[str, str]:
        try:
            return {"email": json.loads(address.state or "")["email"]}
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported("mtempmail needs the inbox cookie issued by generate()") from exc

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        allowed = _allowed(opts)
        resp = await self.http.send(
            "POST", f"{BASE}/get_messages", json={"_token": "", "captcha": ""}, headers=_XHR
        )
        data = self._parse(resp.text)
        minted = self._address(data, resp)
        local, _, domain = minted.value.partition("@")
        if opts.local is None and domain in allowed:
            return minted
        target = domain if domain in allowed else random.choice(allowed)
        return await self._change(opts.local or local, target, self._cookie(minted))

    async def _change(self, local: str, domain: str, inbox: dict[str, str]) -> Address:
        home = await self.http.send("GET", f"{BASE}/")
        csrf, session = _CSRF.search(home.text), home.cookies.get(SESSION)
        if not csrf or not session:
            raise SchemaDrift(
                self.name,
                expected="a CSRF token and session from the home page",
                got=home.text[:200],
            )
        token = csrf.group(1)
        resp = await self.http.send(
            "POST",
            f"{BASE}/change",
            json={"_token": token, "name": local, "domain": domain},
            headers={**_XHR, "X-CSRF-TOKEN": token},
            cookies={SESSION: session, **inbox},
        )
        if resp.status_code == 422:
            raise NotSupported(f"mtempmail refused {local}@{domain}: {resp.text[:200]}")
        return self._address(self._parse(resp.text), resp)

    async def _messages(self, address: Address) -> list[dict]:
        data = self._inbox(
            await self.http.json(
                "POST",
                f"{BASE}/get_messages",
                json={"_token": "", "captcha": ""},
                headers=_XHR,
                cookies=self._cookie(address),
            )
        )
        if data["mailbox"] != address.value:
            raise AddressExpired(f"mtempmail: {address.value} is gone")
        return data["messages"]

    async def list(self, address: Address) -> list[MessageRow]:
        return [_row(m) for m in await self._messages(address)]

    async def get(self, address: Address, id: str) -> Message:
        found = next((m for m in await self._messages(address) if m["id"] == id), None)
        if found is None:
            raise MessageGone(f"mtempmail: message {id} is gone")
        row = _row(found)
        is_html = found.get("html") is not False
        html = found["content"] if is_html else ""
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html) if is_html else found["content"],
            links=extract_links(html),
        )


def _row(m: dict) -> MessageRow:
    name, email = m.get("from") or "", m.get("from_email") or ""
    return MessageRow(
        id=m["id"],
        sender=f"{name} <{email}>" if name and email else name or email,
        subject=m.get("subject") or "",
        received_at=_received(m),
    )
