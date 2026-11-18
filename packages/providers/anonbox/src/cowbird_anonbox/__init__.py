"""anonbox.net provider (CCC Berlin).

qmail + lighttpd + shell CGI. Zero auth, no JS, no session, no captcha:
``GET /en`` renders a fresh random address plus a secret mbox URL
(``https://anonbox.net/{h1}/{h2}``); fetching that URL returns the whole
mailbox as a ``text/plain`` mbox (0 bytes when empty).

**The destructive-read hazard.** A per-minute crontab (``delete.sh``) parses
the lighttpd access log and *deletes mailboxes once they have been fetched*.
A re-fetch usually still works for a short while, but the only reliable read
is the first one. This adapter therefore never "polls then reads": ``list()``
parses the fetched mbox into header rows, and ``get()`` re-fetches the same
URL and re-parses to find the one message — two fetches per message read, so
callers should treat the first ``list()`` as authoritative and read bodies
immediately. Addresses are valid for 1 day (nightly rebuild rotates qmail's
``rcpthosts``).

The ``/en`` page hides decoy address fragments in invisible ``<dd>`` blocks
and ``display: none`` spans (a fake ``…@5at52.googlemail.com`` and junk
local-part fragments). ``parse_page`` strips every hidden element and every
span before extracting the address, so decoys can never leak into
``Address.value``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from email import message_from_string
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://anonbox.net"

_HIDDEN_DD = re.compile(r"<dd[^>]*display\s*:\s*none[^>]*>.*?</dd>", re.S | re.I)
_SPAN = re.compile(r"<span[^>]*>.*?</span>", re.S | re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MBOX_LINK = re.compile(r'href="' + re.escape(SITE) + r"/([A-Za-z0-9]+/[A-Za-z0-9]+)\"")
# mboxo: a body line that began with "From " is stored as ">From ".
_FROM_ESCAPE = re.compile(r"^>From ", re.M)
_FROM_SEPARATOR = re.compile(r"^From .*$", re.M)
_VALID_UNTIL = re.compile(r"(\d{2}/\d{2}/\d{2})\s+(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", re.I)


def _strip_hidden(html: str) -> str:
    """Remove decoy carriers: hidden dd blocks, then every span, then tags."""
    html = _HIDDEN_DD.sub("", html)
    html = _SPAN.sub("", html)
    return html


def parse_page(html: str) -> tuple[str, str]:
    """Extract (address, mbox path) from the /en page, decoys removed."""
    visible = _strip_hidden(html)
    email = _EMAIL.search(visible)
    link = _MBOX_LINK.search(visible)
    if not email or not link:
        raise SchemaDrift(
            "anonbox",
            expected="a random address and a /{h1}/{h2} mbox link in the /en page",
            got=f"email={bool(email)} link={bool(link)}",
        )
    return email.group(0), link.group(1)


class RawMail:
    __slots__ = ("id", "sender", "subject", "received_at", "body", "html", "message_id")

    def __init__(self, id, sender, subject, received_at, body, html, message_id=""):  # noqa: ANN001
        self.id, self.sender, self.subject = id, sender, subject
        self.received_at, self.body, self.html = received_at, body, html
        self.message_id = message_id


def _decode(part) -> str:  # noqa: ANN001
    payload = part.get_payload(decode=True)
    if payload is None:
        return part.get_payload() or ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except (LookupError, UnicodeError):
        # A mislabeled or exotic charset must degrade, not kill the read:
        # one bad message would otherwise re-raise on every poll forever.
        return payload.decode("utf-8", "replace")


def _bodies(msg) -> tuple[str, str]:  # noqa: ANN001
    """(text, html) from a message, walking multipart alternatives."""
    text = html = ""
    parts = msg.walk() if msg.is_multipart() else (msg,)
    for part in parts:
        if part.is_multipart():
            continue
        content = _FROM_ESCAPE.sub("From ", _decode(part))
        ctype = part.get_content_type()
        if ctype == "text/html" and not html:
            html = content
        elif ctype == "text/plain" and not text:
            text = content
    if html and not text:
        text = html_to_text(html)
    return text, html


def parse_mbox(raw: str) -> list[RawMail]:
    """Split a text/plain mbox into messages. [] for an empty mailbox."""
    if not raw.strip():
        return []
    # Each chunk after a "From " separator line starts with the newline that
    # ended the separator; the email parser is fine with a leading blank line,
    # but strip it so error excerpts stay readable.
    mails: list[RawMail] = []
    for chunk in _FROM_SEPARATOR.split(raw):
        if not chunk.strip():
            continue
        msg = message_from_string(chunk.lstrip("\n"))
        sender_raw = msg.get("From")
        if sender_raw is None:
            raise SchemaDrift(
                "anonbox", expected="a From: header in each mbox message", got=chunk[:200]
            )
        try:
            sender = parseaddr(str(make_header(decode_header(sender_raw))))[1]
        except (LookupError, UnicodeError):
            # Unknown charset in an encoded word: keep the raw header rather
            # than failing the whole inbox read.
            sender = parseaddr(str(sender_raw))[1]
        subject = ""
        if msg.get("Subject") is not None:
            try:
                subject = str(make_header(decode_header(msg.get("Subject"))))
            except Exception:
                subject = msg.get("Subject")
            subject = re.sub(r"\s+", " ", subject).strip()
        received_at = None
        if msg.get("Date"):
            try:
                received_at = parsedate_to_datetime(msg.get("Date"))
            except (TypeError, ValueError):
                received_at = None
        body, html = _bodies(msg)
        message_id = msg.get("Message-ID") or ""
        if not message_id:
            digest = hashlib.sha1(f"{sender}\x00{subject}\x00{chunk}".encode()).hexdigest()
            message_id = f"<{digest}@anonbox>"
        mails.append(RawMail(message_id, sender, subject, received_at, body, html, message_id))
    return mails


def _valid_until(html: str, now: datetime) -> datetime | None:
    """The page shows '11/16/26 11:59 p.m.'; parse it, fall back to +1 day."""
    match = _VALID_UNTIL.search(_strip_hidden(html))
    if not match:
        return now + timedelta(days=1)
    month, day, year = (int(part) for part in match.group(1).split("/"))
    hour = int(match.group(2)) % 12 + (12 if match.group(4).lower() == "p" else 0)
    return datetime(2000 + year, month, day, hour, int(match.group(3)), tzinfo=UTC)


class Anonbox(Provider):
    """anonbox.net — zero-auth mbox over HTTP, destructive-read semantics."""

    name = "anonbox"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("anonbox.net",),
        # Addresses live on per-mailbox subdomains (5at52.anonbox.net, ...);
        # the hash rotates nightly, so no stable domain list can be declared.
        domains=(),
        domain_count=0,
        address_ttl=timedelta(days=1),
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        # The secret mbox path IS the credential: anyone holding /{h1}/{h2}
        # reads the mailbox. It travels in Address.state.
        needs_state=True,
        inbox_privacy=InboxPrivacy.PRIVATE,
        poll_interval=30.0,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        if opts and (opts.local or opts.domain):
            raise NotSupported("anonbox issues random addresses only")
        html = await self.http.text("GET", f"{SITE}/en/")
        address, path = parse_page(html)
        return Address(
            value=address,
            provider=self.name,
            expires_at=_valid_until(html, datetime.now(UTC)),
            state=json.dumps({"path": path}),
        )

    def _mbox_url(self, address: Address) -> str:
        try:
            path = json.loads(address.state or "")["path"]
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported("anonbox needs the state issued by generate()") from exc
        return f"{SITE}/{path}/"

    async def _fetch(self, address: Address) -> list[RawMail]:
        # send(), not text(): the delete crontab removes fetched mailboxes, and
        # the 404 HTML page must surface as MessageGone, not as a parse error.
        resp = await self.http.send("GET", self._mbox_url(address))
        if resp.status_code == 404:
            raise MessageGone(
                f"anonbox: mailbox {self._mbox_url(address)} is gone "
                "(deleted after its first fetch)"
            )
        return parse_mbox(resp.text)

    async def list(self, address: Address) -> list[MessageRow]:
        # First read is the only reliable read (per-minute delete crontab);
        # parse headers only, so the caller can decide what to re-fetch.
        return [
            MessageRow(
                id=mail.id,
                sender=mail.sender,
                subject=mail.subject,
                received_at=mail.received_at,
            )
            for mail in await self._fetch(address)
        ]

    async def get(self, address: Address, id: str) -> Message:
        # Re-fetch fast: the mailbox may be deleted a minute after the first
        # fetch, so get() goes straight back to the mbox URL.
        for mail in await self._fetch(address):
            if mail.id == id:
                return Message(
                    id=mail.id,
                    sender=mail.sender,
                    subject=mail.subject,
                    received_at=mail.received_at,
                    html=mail.html,
                    text=mail.body,
                )
        raise MessageGone(f"anonbox: message {id} not in the mbox (it expires with the mailbox)")
