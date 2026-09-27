"""receivemail.org — a PHP web client over one catch-all IMAP box.

Inboxes are public: anyone can open any address on these domains and read its
mail, so do not use one for anything you would not post.

The inbox is whichever address user.php last bound to the PHP session.
generate() asks user.php for an address (random, or one it names) and keeps
the PHPSESSID it sets; on one shared cookie jar a second generate() would
rebind the first caller's session, so every request runs on a fresh session
and the session id travels in Address.state.

mail.php renders the whole inbox as HTML: one `div#mail<id>` per message, the
subject and sender in its accordion button, the date (UTC) in an `em` title and
the body in an iframe srcdoc. There is no per-message endpoint, so get() reads
the same page. PHP drops idle sessions, and mail.php then answers "you cleared
your browser cookies"; user.php binds any session id it is sent, so list()
binds the old one to the address again and reads once more.

actions.php?action=download hands out an .eml URL, but it answered
"UnAuthorized" for some sessions and the URL served the home page, so bodies
come from the srcdoc instead.
"""

from __future__ import annotations

import html as htmllib
import random
import re
import secrets
from datetime import UTC, datetime, timedelta

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://www.receivemail.org"
DOMAINS = ("getsomail.com", "ofisher.net", "barooko.com")

_ADDRESS = re.compile(r"[^@\s<>]+@[^@\s<>]+")
_BLOCK = re.compile(r'<div id="mail(\d+)">(.*?)(?=<div id="mail\d+">|\Z)', re.S)
_HEAD = re.compile(r'<button class="accordion">(.*?)<br>From : (.*?)</button>', re.S)
_DATE = re.compile(r'<em title="([^"]+)"')
_BODY = re.compile(r'srcdoc="([^"]*)"')
# What an empty inbox renders: the site's spinner and nothing else.
_EMPTY = "cssload-container"
_NO_SESSION = "cleared your <b>browser cookies</b>"


def _at(block: str) -> datetime | None:
    match = _DATE.search(block)
    try:
        return datetime.strptime(match.group(1), "%d-%m-%Y %I:%M:%S %p").replace(tzinfo=UTC)
    except (AttributeError, ValueError):
        return None


class ReceiveMail(Provider):
    name = "receivemail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("receivemail.org",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        # Any address can be opened at any time; nothing expires it.
        address_ttl=None,
        # A floor, not measured: the shared test@ inbox held a 3-day-old message.
        message_ttl=timedelta(days=3),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        max_concurrency=1,
        fresh_session=True,
    )

    async def _bind(self, user: str, session: str | None = None) -> tuple[str, str | None]:
        """Bind `user` ("" for a random address) to a session; the address and
        the PHPSESSID the site set, if it set one."""
        resp = await self.http.send(
            "GET",
            f"{SITE}/user.php",
            params={"user": user},
            cookies={"PHPSESSID": session} if session else None,
        )
        value = resp.text.strip()
        if not _ADDRESS.fullmatch(value):
            raise SchemaDrift(self.name, expected="an address from user.php", got=value[:200])
        return value, resp.cookies.get("PHPSESSID")

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in DOMAINS:
            raise NotSupported(f"receivemail does not serve {opts.domain}")
        wanted = ""
        if opts.local or opts.domain:
            # user.php ignores a bare "@domain", so a domain alone gets a local part here.
            local = opts.local or secrets.token_hex(5)
            wanted = f"{local}@{opts.domain or random.choice(DOMAINS)}".lower()
        value, session = await self._bind(wanted)
        if wanted and value != wanted:
            raise NotSupported(f"receivemail would not issue {wanted}; it offered {value}")
        if not session:
            raise SchemaDrift(self.name, expected="a PHPSESSID from user.php", got=value)
        return Address(value=value, provider=self.name, state=session)

    async def _blocks(self, address: Address) -> dict[str, str]:
        if not address.state:
            raise NotSupported("receivemail needs the session issued by generate()")
        cookies = {"PHPSESSID": address.state}
        page = await self.http.text("GET", f"{SITE}/mail.php", cookies=cookies)
        if _NO_SESSION in page:
            await self._bind(address.value, address.state)
            page = await self.http.text("GET", f"{SITE}/mail.php", cookies=cookies)
            if _NO_SESSION in page:
                raise SchemaDrift(
                    self.name, expected="user.php to bind the session", got=page[:200]
                )
        blocks = dict(_BLOCK.findall(page))
        if not blocks and _EMPTY not in page:
            raise SchemaDrift(self.name, expected="div#mail<id> blocks", got=page[:200])
        return blocks

    def _row(self, id: str, block: str) -> MessageRow:
        head = _HEAD.search(block)
        if not head:
            raise SchemaDrift(self.name, expected="a subject and sender", got=block[:200])
        sender = re.sub(r"(?<=\S)<", " <", htmllib.unescape(head.group(2)).strip(), count=1)
        return MessageRow(
            id=id,
            sender=sender,
            subject=htmllib.unescape(head.group(1)).strip(),
            received_at=_at(block),
        )

    async def list(self, address: Address) -> list[MessageRow]:
        return [self._row(id, block) for id, block in (await self._blocks(address)).items()]

    async def get(self, address: Address, id: str) -> Message:
        block = (await self._blocks(address)).get(id)
        if block is None:
            raise MessageGone(f"receivemail: message {id} is gone")
        row, body = self._row(id, block), _BODY.search(block)
        # Only HTML mail was seen with a srcdoc. Without one, the body is
        # whatever the panel renders after the date line, not a changed page.
        html = htmllib.unescape(body.group(1)) if body else block.partition("</p>")[2]
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
