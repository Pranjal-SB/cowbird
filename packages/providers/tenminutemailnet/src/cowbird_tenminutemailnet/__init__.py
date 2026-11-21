"""10minutemail.net — PHP + jQuery site on its own mail domain (zefvo.com).

Distinct from 10minutemail.com despite the name. Everything is a GET of an
HTML page or fragment, keyed by the `TENMM_G1_SESSION` cookie the front door
sets. The transport runs with `fresh_session` (one throwaway session per
request), so the adapter captures the cookie at generate() and replays it as
an explicit `Cookie` header on every later call.

An address is never requested — it is issued by the server on `GET /` and
rendered into `input#fe_text`. The same page embeds a base64 `data-history`
blob: JSON with the mailbox's permalink URL and access key, the portable
credential pair for recovering the mailbox after the session cookie dies.

The service delivers its own welcome mail on generate, which is how the full
generate → list → read loop was proven live without an external sender
(2026-11-15). Fixtures were recorded through a real `Transport` with
`fresh_session=True`, exactly as the runtime drives it.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from urllib.parse import urljoin

from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://10minutemail.net"
TTL = timedelta(minutes=10)

_ADDRESS = re.compile(r'id="fe_text"[^>]*value="([^"]+@[^"]+)"')
_HISTORY = re.compile(r'data-history="([^"]+)"')
_ROW = re.compile(
    r'<a class="row-link" href="readmail\.html\?mid=(?P<id>[^"]+)">'
    r'(?P<sender>[^<]*)</a></td><td><a class="row-link" href="readmail\.html\?mid='
    r'(?P=id)">(?P<subject>[^<]*)</a>'
)
_SUBJECT = re.compile(r'<h2 class="emoji_parse">(.*?)</h2>', re.S)
_SENDER = re.compile(r'<span class="mail_from">(.*?)</span>', re.S)
_TIMESTAMP = re.compile(r'<span title="(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"')
_BODY = re.compile(r'<div class="mailinhtml">(.*?)</div>\s*</div>', re.S)
_TAG = re.compile(r"<[^>]+>")
# Cloudflare's email-protection cloaks every address on the page as
# <a data-cfemail="...">[email protected]</a>; the hex is XOR-encoded with its
# own first byte.
_CFEMAIL = re.compile(r'<a[^>]*data-cfemail="([0-9a-f]+)"[^>]*>[^<]*</a>')


def _drift(expected: str, got: object) -> SchemaDrift:
    return SchemaDrift("tenminutemailnet", expected=expected, got=got)


def _decode_cfemail(encoded: str) -> str:
    key = int(encoded[:2], 16)
    return "".join(chr(int(encoded[i : i + 2], 16) ^ key) for i in range(2, len(encoded), 2))


def _strip_tags(html: str) -> str:
    return _TAG.sub("", _CFEMAIL.sub(lambda m: _decode_cfemail(m[1]), html)).strip()


class TenMinuteMailNet(Provider):
    name = "tenminutemailnet"
    api: ClassVar[str] = SITE
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("10minutemail.net",),
        domains=("zefvo.com",),  # the only MX-bearing domain; 10minutemail.net itself has none
        domain_count=1,
        address_ttl=TTL,
        message_ttl=TTL,  # messages die with the mailbox (extendable to 100 min)
        push=False,
        delete=False,  # only new-address rotation; no per-message delete endpoint
        custom_local=False,  # server-issued 8-char alnum local part
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        fresh_session=True,
        inbox_privacy=InboxPrivacy.PRIVATE,  # mailbox keyed by session cookie / permalink+key
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in self.caps.domains:
            # A caller typo must not quarantine a healthy provider, and it is
            # not reroutable: no sibling serves this domain either.
            raise NotSupported(
                f"{self.name} does not serve {opts.domain}; has {list(self.caps.domains)}"
            )
        resp = await self.http.send("GET", f"{SITE}/")
        if resp.status_code != 200:
            # A blocked front door is a backend problem, not a missing message:
            # ProviderDown is reroutable and counts against health; MessageGone
            # would leave the provider "healthy" while nothing works.
            raise ProviderDown(f"{self.name}: front door returned HTTP {resp.status_code}")
        address = self._parse_address(resp.text)
        cookie = resp.cookies.get("TENMM_G1_SESSION")
        history = self._parse_history(resp.text)
        if not cookie:
            # Without the session cookie no later call can reach this mailbox.
            raise _drift("a TENMM_G1_SESSION cookie from GET /", dict(resp.cookies))
        state = json.dumps(
            {
                "address": address,
                "cookie": cookie,
                "permalink": history["url"],
                "key": history["key"],
            }
        )
        return Address(
            value=address,
            provider=self.name,
            expires_at=datetime.now(UTC) + TTL,
            state=state,
        )

    async def list(self, address: Address) -> list[MessageRow]:
        html = await self.http.text(
            "GET", f"{SITE}/mailbox.ajax.php", headers=self._headers(address)
        )
        # An empty inbox is the header row only; a non-table response (login
        # page, error text) means the session died or the shape moved.
        if 'id="maillist"' not in html:
            if address.is_expired():
                # Past the backend's ten minutes the session is simply gone.
                raise AddressExpired(f"tenminutemailnet: {address.value} has expired")
            raise _drift('a <table id="maillist"> fragment', html[:200])
        rows = [
            MessageRow(
                id=match["id"],
                sender=_strip_tags(match["sender"]),
                subject=_strip_tags(match["subject"]),
                received_at=None,  # the fragment only says "just now"/"2 min ago"
            )
            for match in _ROW.finditer(html)
        ]
        if not rows and "readmail.html?mid=" in html:
            # The table rendered but the row markup moved: an empty inbox is the
            # header row only, so anchors present yet unparsed is drift, not [].
            raise _drift("parsable rows in the maillist fragment", html[:200])
        return rows

    async def get(self, address: Address, id: str) -> Message:
        resp = await self.http.send(
            "GET", f"{SITE}/readmail.html?mid={id}", headers=self._headers(address)
        )
        if resp.status_code == 404:
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code >= 400:
            # Any other 4xx is an access/server problem on the read page, not
            # a shape change: parsing it would misreport it as schema drift.
            raise ProviderDown(f"{self.name}: readmail returned HTTP {resp.status_code}")
        html = resp.text
        subject = _SUBJECT.search(html)
        sender = _SENDER.search(html)
        stamp = _TIMESTAMP.search(html)
        body = _BODY.search(html)
        if not (subject and sender and body):
            missing = [
                n for n, m in (("subject", subject), ("sender", sender), ("body", body)) if not m
            ]
            raise _drift(f"{missing} in the readmail page", html[:200])
        received = (
            datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) if stamp else None
        )
        body_html = body[1]
        return Message(
            id=id,
            sender=_strip_tags(sender[1]),
            subject=_strip_tags(subject[1]),
            received_at=received,
            html=body_html,
            text=html_to_text(body_html),
            links=tuple(urljoin(f"{SITE}/", link) for link in extract_links(body_html)),
        )

    @staticmethod
    def _headers(address: Address) -> dict[str, str]:
        try:
            cookie = json.loads(address.state or "")["cookie"]
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported("tenminutemailnet needs the state issued by generate()") from exc
        return {"Cookie": f"TENMM_G1_SESSION={cookie}"}

    @staticmethod
    def _parse_address(html: str) -> str:
        match = _ADDRESS.search(html)
        if not match:
            raise _drift("input#fe_text with the issued address", html[:200])
        return match[1]

    @staticmethod
    def _parse_history(html: str) -> dict:
        match = _HISTORY.search(html)
        if not match:
            raise _drift("the data-history permalink blob", html[:200])
        try:
            history = json.loads(base64.b64decode(match[1]))
        except (ValueError, json.JSONDecodeError) as exc:
            raise _drift("a base64 JSON data-history blob", match[1][:100]) from exc
        for field in ("url", "key"):
            if field not in history:
                raise _drift(f"{field!r} in data-history", list(history))
        return history
