"""mailnesia.com provider.

Server-rendered HTML, no auth, no cookies: any local part @mailnesia.com
exists implicitly, so generate() never touches the network and the address
itself is the only state. Fixtures were recorded through a real `Transport`
and neutralized (senders/subjects/bodies replaced).

Quirk: common local parts (test, admin, info, facebook, ...) are *aliases* of
other mailboxes — the mailbox page renders a warning and no messages. Those
addresses can never receive for the caller, so list() raises NotSupported
instead of masquerading as a healthy empty inbox. Random locals never alias.

mailnesia's server visits links in incoming mail itself, so single-use
verification links can be consumed before the caller opens the message.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import ClassVar

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://mailnesia.com"

# mailnesia.com plus the five yearly-rotating alternates observed 2026-11-15;
# they expire ~Nov 29 each year and rotate, so this tuple needs a refresh then.
DOMAINS = (
    "mailnesia.com",
    "airmailed.shop",
    "bulkfinder.site",
    "emaill.mom",
    "mailed.click",
    "poofmail.fit",
)

_ALIAS_MARKER = "alias of another mailbox"
# One inbox-table row: the id sits on the <tr>, the cells are uniform anchors.
_ROW = re.compile(
    r'<tr id="(?P<id>\d+)" class="emailheader".*?</tr>', re.DOTALL
)
_TIME = re.compile(r'<time datetime="([^"]+)"')
_ANCHOR = re.compile(r'<a class="email"[^>]*>(.*?)</a>', re.DOTALL)
_ADDRESS = re.compile(r"<([^<>]+@[^<>]+)>")
_BODY = re.compile(r'<div class="body">(.*)<div id="footer">', re.DOTALL)
_RSS_ITEM = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_RSS_LINK = re.compile(r"<link>(.*?)</link>")
_RSS_TITLE = re.compile(r"<title>(.*?)</title>")
_LOCAL = re.compile(r"[a-z0-9._+-]{1,30}")


def _strip_tags(fragment: str) -> str:
    return html_to_text(fragment)


def _sender(raw: str) -> str:
    """The From cell holds `Name <addr>` or a bare address."""
    m = _ADDRESS.search(raw)
    return m.group(1) if m else raw


def _at(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _local_of(address: Address) -> str:
    local = address.value.split("@", 1)[0]
    if not _LOCAL.fullmatch(local):
        raise NotSupported(f"mailnesia: not a usable local part: {local!r}")
    return local


class Mailnesia(Provider):
    name = "mailnesia"
    api: ClassVar[str] = API
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("mailnesia.com",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        address_ttl=None,
        message_ttl=timedelta(days=2),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=True,  # open-source (perl); BYOD via MX at mailnesia.com
        needs_residential_ip=False,
        needs_state=False,
        inbox_privacy=InboxPrivacy.PUBLIC,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        domain = opts.domain or DOMAINS[0]
        if opts.domain and opts.domain not in DOMAINS:
            # A caller asking for a domain this backend does not serve is a
            # caller error, not upstream drift. BYOD exists upstream, but this
            # adapter cannot verify a foreign domain's MX, so it refuses.
            raise NotSupported(f"{self.name} does not serve {opts.domain}; has {list(DOMAINS)}")
        local = opts.local or f"cb{secrets.token_hex(5)}"
        if not _LOCAL.fullmatch(local):
            # Core allows [A-Za-z0-9._+-]{1,64}; mailnesia's rule is stricter
            # ([a-z0-9._+-]{1,30}). Minting a local part list() would refuse
            # would hand the caller an address that errors on first use.
            raise NotSupported(f"mailnesia: not a usable local part: {local!r}")
        return Address(value=f"{local}@{domain}", provider=self.name)

    async def list(self, address: Address) -> list[MessageRow]:
        html = await self.http.text("GET", f"{self.api}/mailbox/{_local_of(address)}")
        if _ALIAS_MARKER in html:
            # The caller (or whoever minted this address) picked an aliased
            # local part: mailnesia will never show it mail.
            raise NotSupported(
                f"{self.name}: {address.value} is an alias of another mailbox "
                "and cannot receive; pick an uncommon local part"
            )
        table = re.search(r'<table[^>]*class="email"', html)
        if table is None:
            raise SchemaDrift(
                self.name, expected='an inbox <table class="email">', got=html[:200]
            )
        rows = []
        for m in _ROW.finditer(html):
            cells = _ANCHOR.findall(m.group(0))
            if len(cells) < 3:
                raise SchemaDrift(
                    self.name,
                    expected="Date/From/Subject cells in an inbox row",
                    got=len(cells),
                )
            when = _TIME.search(m.group(0))
            rows.append(
                MessageRow(
                    id=m.group("id"),
                    sender=_sender(_strip_tags(cells[0])),
                    subject=_strip_tags(cells[2]),
                    received_at=_at(when.group(1)) if when else None,
                )
            )
        return rows

    async def get(self, address: Address, id: str) -> Message:
        local = _local_of(address)
        resp = await self.http.send("GET", f"{self.api}/mailbox/{local}/{id}")
        if resp.status_code == 404:
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code >= 400:
            raise ProviderDown(f"{self.name}: message read failed, HTTP {resp.status_code}")
        html = resp.text
        head = re.search(
            r"<tr class=\"(?:odd|even)\">(.*?)</tr>", html, re.DOTALL
        )
        cells = re.findall(r"<td[^>]*>(.*?)</td>", head.group(1), re.DOTALL) if head else []
        if len(cells) < 4:
            raise SchemaDrift(
                self.name, expected="Date/From/To/Subject cells on the message page",
                got=len(cells),
            )
        body = _BODY.search(html)
        if body is None:
            raise SchemaDrift(self.name, expected='a <div class="body">', got=html[:200])
        message_html = body.group(1)
        try:
            received = parsedate_to_datetime(_strip_tags(cells[0]))
        except (TypeError, ValueError):
            received = None
        return Message(
            id=id,
            sender=_sender(_strip_tags(cells[1])),
            subject=_strip_tags(cells[3]),
            received_at=received,
            html=message_html,
            text=html_to_text(message_html),
            links=extract_links(message_html),
        )

    async def rss(self, address: Address, count: int = 20) -> list[MessageRow]:
        """Bonus feed: the same inbox as RSS 2.0, handy for push-style polling
        from outside the process."""
        xml = await self.http.text(
            "GET", f"{self.api}/rss/{_local_of(address)}", params={"mailcount": str(count)}
        )
        rows = []
        for item in _RSS_ITEM.finditer(xml):
            body = item.group(1)
            link = _RSS_LINK.search(body)
            title = _RSS_TITLE.search(body)
            if link is None or title is None:
                raise SchemaDrift(
                    self.name, expected="<link> and <title> in an RSS <item>",
                    got=body[:200],
                )
            m = re.search(r"/mailbox/[^/]+/(\d+)", link.group(1))
            if m is None:
                raise SchemaDrift(
                    self.name, expected="a /mailbox/<name>/<id> link in RSS", got=link.group(1)
                )
            # Title is "Subject – Sender <addr>".
            subject, _, sender = title.group(1).rpartition(" &#8211; ")
            if not subject:
                subject, _, sender = title.group(1).rpartition(" – ")
            pub = re.search(r"<pubDate>(.*?)</pubDate>", body)
            try:
                received = parsedate_to_datetime(pub.group(1)) if pub else None
            except (TypeError, ValueError):
                received = None
            rows.append(
                MessageRow(
                    id=m.group(1),
                    sender=_sender(_strip_tags(sender)),
                    subject=_strip_tags(subject),
                    received_at=received,
                )
            )
        return rows
