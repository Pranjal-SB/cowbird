"""eyepaste.com — a public inbox read through its RSS feed.

Any local part at eyepaste.com receives, and anyone who knows the address can
read the box: there is no login, no token and no PIN. Callers should use an
unguessable local part; generate() picks a long random one.

The feed is the only machine-readable view (the HTML inbox hides addresses
behind Cloudflare's email obfuscation). Each item carries a From/To/Subject/Date
block, then the mail's raw body with every newline swapped for `<br/>`. The
mail's own headers are gone, so a multipart body arrives as bare boundary
lines. Items have no id, so one is derived from the date, sender and subject.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://www.eyepaste.com"
DOMAIN = "eyepaste.com"
_HTML = re.compile(r"<(html|body|div|p|table|a|br)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Item:
    id: str
    sender: str
    subject: str
    received_at: datetime | None
    body: str


def _repair(feed: str) -> str:
    # The server encodes its UTF-8 bytes as UTF-8 a second time: an em dash
    # arrives as "â\x80\x94". Undo that, and leave a feed alone that it breaks.
    try:
        return feed.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return feed


def _at(value: str) -> datetime | None:
    try:
        return parsedate_to_datetime(value.strip()).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _item(node: ET.Element) -> _Item:
    description = node.findtext("description") or ""
    head, _, rest = description.partition("</p>")
    fields = {}
    for line in head.replace("<p>", "").split("<br/>"):
        key, _, value = line.strip().partition(": ")
        fields[key] = value.strip()
    body = rest.strip().removeprefix("<p>").removesuffix("</p>").strip()
    date = (node.findtext("pubdate") or fields.get("Date", "")).strip()
    sender, subject = fields.get("From", ""), fields.get("Subject", "")
    digest = hashlib.sha256(f"{date}\n{sender}\n{subject}".encode()).hexdigest()[:16]
    return _Item(
        id=digest,
        sender=sender,
        subject=subject,
        received_at=_at(date),
        body=body.replace("<br/>", "\n").lstrip("﻿"),
    )


def _split(body: str) -> tuple[str, str]:
    """(html, text) from a body whose own headers were stripped."""
    first = body.split("\n", 1)[0].strip()
    if first.startswith("--") and len(first) > 2:
        source = f'Content-Type: multipart/mixed; boundary="{first[2:]}"\n\n{body}'
        # Bytes, not str: a str source with non-ASCII text comes back
        # raw-unicode-escaped ("—") from get_content().
        parsed = email.message_from_bytes(source.encode("utf-8"), policy=email.policy.default)
        parts = [parsed.get_body(preferencelist=(kind,)) for kind in ("html", "plain")]
        try:
            html, text = (p.get_content() if p is not None else "" for p in parts)
        except LookupError as exc:
            raise SchemaDrift("eyepaste", expected="a charset Python knows", got=str(exc)) from exc
        return html, text or html_to_text(html)
    if _HTML.search(body):
        return body, html_to_text(body)
    return "", body


class Eyepaste(Provider):
    name = "eyepaste"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=(DOMAIN,),
        domains=(DOMAIN,),
        domain_count=1,
        address_ttl=None,
        # The site says mail drops after an hour; mail 35 hours old was still
        # in a feed when this adapter was written.
        message_ttl=timedelta(hours=1),
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain != DOMAIN:
            raise NotSupported(f"eyepaste does not serve {opts.domain}")
        local = opts.local or f"cb{secrets.token_hex(8)}"
        return Address(value=f"{local}@{DOMAIN}", provider=self.name)

    async def _items(self, address: Address) -> list[_Item]:
        feed = _repair(await self.http.text("GET", f"{SITE}/inbox/{address.value}.rss"))
        # RSS never needs a DTD, and a DTD is the only way entity expansion
        # reaches the parser. Refuse it rather than trust the parser's limits.
        # Only the prolog counts: a mail body in CDATA may carry its own
        # <!DOCTYPE html>.
        prolog = feed.partition("<rss")[0].lower()
        if "<!doctype" in prolog or "<!entity" in prolog:
            raise SchemaDrift(self.name, expected="an RSS feed without a DTD", got=feed[:200])
        try:
            channel = ET.fromstring(feed).find("channel")
        except ET.ParseError as exc:
            raise SchemaDrift(self.name, expected="an RSS feed", got=feed[:200]) from exc
        if channel is None:
            raise SchemaDrift(self.name, expected="an RSS <channel>", got=feed[:200])
        return [_item(node) for node in channel.iter("item")]

    async def list(self, address: Address) -> list[MessageRow]:
        return [
            MessageRow(id=i.id, sender=i.sender, subject=i.subject, received_at=i.received_at)
            for i in await self._items(address)
        ]

    async def get(self, address: Address, id: str) -> Message:
        # ponytail: re-reads the whole feed per message; the feed is the only source.
        item = next((i for i in await self._items(address) if i.id == id), None)
        if item is None:
            raise MessageGone(f"eyepaste: message {id} is gone")
        html, text = _split(item.body)
        return Message(
            id=id,
            sender=item.sender,
            subject=item.subject,
            received_at=item.received_at,
            html=html,
            text=text,
            links=extract_links(html or text),
        )
