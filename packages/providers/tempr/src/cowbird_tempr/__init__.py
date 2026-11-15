"""tempr.email provider — cowbird's first push provider.

tempr.email is a server-rendered nginx site with no Cloudflare and no
server-side captcha gate. Two hosts matter: `tempr.email` for address
acquisition, and `mta.trashmailr.com:81` (h2-only, nonstandard port) for the
live inbox watch and message reads, which speak Datastar SSE rather than REST.

The watch is a long-lived SSE stream with **no heartbeat**: the server's first
frame re-patches the list div with the same `data-init="@get('...watch...')"`
attribute, which is what makes a real browser reconnect. This adapter does the
same thing explicitly: read the stream with an idle bound, reconnect on
silence. Every fixture under tests/fixtures is a raw 2026-11-15 capture made
through the same curl_cffi h2 client this adapter runs
(docs/recon/tempr-followup.md).
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import CurlError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

SITE = "https://tempr.email"
MTA = "https://mta.trashmailr.com:81"

# Datastar's client sends these exact headers, and serializes the page's
# signal state into a `datastar=` query parameter. Without that parameter the
# mta endpoints 500 identically every time — a failure that looks like an auth
# problem but is request shaping (recon §2).
SSE_HEADERS = {
    "Accept": "text/event-stream, text/html, application/json",
    "Datastar-Request": "true",
}
SIGNALS = {"tz": "Europe/Berlin", "language": "en"}

_CSRF_RE = re.compile(r'name="login-csrf"\s+value="([^"]+)"')
_DOMAIN_RE = re.compile(r'<option value="([^"]+)"')


@dataclass(frozen=True, slots=True)
class Patch:
    """One complete SSE event from the Datastar stream.

    `elements` concatenates the frame's `data:elements` payloads (Datastar
    writes them inline, several per frame, one element line each). Frames of
    other kinds (e.g. `datastar-patch-signals`) parse to an empty `elements` —
    this adapter only consumes element patches.
    """

    event: str
    elements: str = ""


def parse_sse(text: str) -> list[Patch]:
    """Parse raw SSE text into Datastar patches. Empty/idle input → [].

    Pure function over captured bytes so the framing can be tested without a
    network. Datastar's framing differs from textbook SSE in two ways that
    matter: no space after `event:`/`data:`, and element payloads inline on
    the `data:` line rather than in continuation lines.
    """
    patches: list[Patch] = []
    event = ""
    elements: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            # Blank line = frame terminator (SSE). Datastar frames contain no
            # internal blank lines, so a flush here is always a frame end.
            if event or elements:
                patches.append(Patch(event=event, elements="\n".join(elements)))
            event, elements = "", []
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            rest = line[len("data:"):]
            if rest.startswith("elements "):
                elements.append(rest[len("elements "):])
    if event or elements:
        patches.append(Patch(event=event, elements="\n".join(elements)))
    return patches


def _pack(sid: str, csrf: str) -> str:
    """The session identity. tempr keys the inbox on the `sid` cookie (90d);
    the CSRF token from the login page is replayed on the acquisition POST.
    Nothing in this adapter reads it back — every protocol call keys on the
    address path — but the state is what makes the acquisition reproducible
    out of process, and `needs_state=True` promises it."""
    return json.dumps({"sid": sid, "csrf": csrf})


# A persistent mta outage must surface, not spin forever: after this many
# consecutive failed reconnects, watch() lets the ProviderDown propagate.
MAX_RECONNECTS = 3


_ROW_SPLIT = re.compile(r'<div data-message="([^"]+)" data-read="(\w+)"')
_ABOUT = re.compile(r'<span>([^<]*)</span>\s*<span>([^<]*)</span>')
_LABEL = re.compile(r"<label>(\w+):</label>\s*<div>(.*?)</div>", re.S)
_SRCDOC = re.compile(r'srcdoc="(.*?)"\s*frameborder', re.S)
_BERLIN = ZoneInfo("Europe/Berlin")


def _rows(elements: str) -> list[MessageRow]:
    """Message rows out of a patched inbox list.

    One row (captured, see fixtures/sse_message.txt):

        <div data-message="01M6NWK..." data-read="false">
          <div class="selection">... checkbox value=<id> ...</div>
          <div class="status" data-on:click="@patch('...?show=<id>')">...icon...</div>
          <div class="about" ...><span>sender</span> <span>subject</span></div>
          <div class="datetime" ...><span>05.11.2026</span><span>13:57:55</span></div>
          <div class="option" ... data-on:click="@delete('.../<id>')">...</div>
        </div>
    """
    rows: list[MessageRow] = []
    parts = _ROW_SPLIT.split(elements)
    # parts: [pre] id, read, body, id, read, body, ...
    for i in range(1, len(parts), 3):
        id_, body = parts[i], parts[i + 2]
        about = body.split('class="datetime"')[0]
        spans = _ABOUT.findall(about)
        sender, subject = (spans[0] if spans else ("", ""))
        when = re.search(
            r'class="datetime".*?<span>([\d.]+)</span>\s*<span>([\d:]+)</span>',
            body, re.S,
        )
        received = None
        if when:
            try:
                received = datetime.strptime(
                    f"{when.group(1)} {when.group(2)}", "%d.%m.%Y %H:%M:%S"
                ).replace(tzinfo=_BERLIN)
            except ValueError:
                received = None
        rows.append(MessageRow(
            id=id_,
            sender=htmllib.unescape(sender).strip(),
            subject=htmllib.unescape(subject).strip(),
            received_at=received,
            locked=False,
        ))
    return rows


def _message_from_patch(patch: Patch, id: str) -> Message:
    """A Message out of the body patch the mta host sends for one message.

    The patch rebuilds `#message-header` (Receiver/Sender/Date/Subject label
    blocks) and `#message-content` (the body inside a sandboxed iframe's
    srcdoc, HTML-escaped).
    """
    elements = patch.elements
    labels = {k.lower(): v for k, v in _LABEL.findall(elements)}
    srcdoc = _SRCDOC.search(elements)
    body = htmllib.unescape(srcdoc.group(1)) if srcdoc else ""
    received = None
    raw_date = ""
    time_m = re.search(r"<time>([^<]*)</time>", elements)
    if time_m:
        raw_date = time_m.group(1).strip()
        # "10 Nov. 2026 14:47" — the language signal is en, so English months.
        months = {m: i for i, m in enumerate(
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
        dm = re.match(r"(\d+) (\w+)\.? (\d+) (\d{2}):(\d{2})", raw_date)
        if dm and dm.group(2)[:3] in months:
            received = datetime(
                int(dm.group(3)), months[dm.group(2)[:3]], int(dm.group(1)),
                int(dm.group(4)), int(dm.group(5)), tzinfo=_BERLIN,
            )
    return Message(
        id=id,
        sender=htmllib.unescape(labels.get("sender", "")).strip(),
        subject=htmllib.unescape(labels.get("subject", "")).strip(),
        received_at=received,
        html=body,
        text=html_to_text(body),
        links=extract_links(body),
    )


class Tempr(Provider):
    name = "tempr"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        # The 60-odd domains are community-contributed and churn; scrape them
        # per generate() rather than pinning a list (recon §5).
        sites=("tempr.email",),
        domains=(),
        domain_count=60,
        address_ttl=None,  # sid cookie lasts 90d; no published address expiry
        message_ttl=timedelta(days=30),  # site copy: "Available for 30 days"
        push=True,
        delete=True,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        fresh_session=True,  # inbox keyed on the sid cookie; no shared jar
        inbox_privacy=InboxPrivacy.PUBLIC,  # address alone unlocks the inbox
        poll_interval=30.0,  # the stream's own idle window
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        resp = await self.http.send("GET", f"{SITE}/en/")
        csrf = _CSRF_RE.search(resp.text)
        if csrf is None:
            raise SchemaDrift(self.name, expected="login-csrf on /en/", got=resp.text[:200])
        domains = _DOMAIN_RE.findall(resp.text)
        if opts.domain and opts.domain not in domains:
            # A caller typo is not upstream drift: refuse without quarantining.
            raise NotSupported(f"{self.name} does not serve {opts.domain}; has {domains[:5]}...")
        domain = opts.domain or domains[0]
        local = opts.local or f"cb{secrets.token_hex(5)}"
        value = f"{local}@{domain}"
        sid = resp.cookies.get("sid", "")

        await self.http.send(
            "POST",
            f"{SITE}/en/inbox/list.htm",
            data={
                "login-alias": local,
                "login-domains": domain,
                "login-owndomain": "",
                "login-password": "",
                "login-domainType": "list",
                "login-submit": "",  # JS fills Date.now(); server ignores it
                "login-hp": "",  # honeypot: must stay empty
                "login-language": "en",
                "login-csrf": csrf.group(1),
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        return Address(value=value, provider=self.name, state=_pack(sid, csrf.group(1)))

    def _watch_url(self, address: Address) -> str:
        return (
            f"{MTA}/app/message/watch/{address.value}?page=1"
            f"&datastar={quote(json.dumps(SIGNALS))}"
        )

    def _session(self) -> AsyncSession:
        """A session matching the Transport's egress: same impersonation and
        proxy. The watch bypasses Transport (no streaming API), but it must
        not bypass its egress — a proxied deployment would otherwise split
        watch traffic off onto the raw network while get() goes through the
        proxy."""
        return AsyncSession(
            impersonate=self.http.impersonate,
            proxies={"http": self.http.proxy, "https": self.http.proxy}
            if self.http.proxy
            else None,
        )

    async def _sse(self, url: str, idle: float) -> AsyncIterator[bytes]:
        """Raw h2 SSE read on the mta host. curl_cffi maps `timeout` onto
        CURLOPT_LOW_SPEED_TIME, so a silent stream is killed after `idle`
        seconds — the same behaviour as a real browser tab losing its watch.
        Do NOT force http/1.1: the :81 vhost answers h2 only (recon §2:
        http/1.1 gets a raw TLS reset).

        curl transport failures translate here: the idle kill is an
        asyncio.TimeoutError (the boundedness contract), every other curl
        error — connect failure, TLS reset, mid-stream drop — is ProviderDown.
        """
        session = self._session()
        try:
            try:
                resp = await session.get(url, headers=SSE_HEADERS, stream=True, timeout=idle)
            except CurlError as exc:
                raise ProviderDown(f"{self.name}: watch connect failed: {exc}") from exc
            if resp.status_code != 200:
                raise ProviderDown(f"{self.name}: watch stream HTTP {resp.status_code}")
            try:
                async for chunk in resp.aiter_content():
                    yield chunk
            except CurlTimeout as exc:
                # The idle bound surfaces as curl (28), not asyncio.TimeoutError.
                raise TimeoutError from exc
            except CurlError as exc:
                raise ProviderDown(f"{self.name}: watch stream dropped: {exc}") from exc
        finally:
            await session.close()

    async def _frames(self, address: Address, idle: float) -> AsyncIterator[Patch]:
        """One watch connection: complete frames until the stream idles out.

        Buffers bytes, not text: 16KB frames arrive in chunks that can split
        a multi-byte UTF-8 character (observed live), so decoding happens per
        complete frame.
        """
        buffer = b""
        async for chunk in self._sse(self._watch_url(address), idle):
            buffer += chunk.encode() if isinstance(chunk, str) else chunk
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                for patch in parse_sse(frame.decode("utf-8", "replace") + "\n\n"):
                    yield patch

    async def watch(self, address: Address, poll: float | None = None) -> AsyncIterator[Message]:
        """Push watch over the Datastar SSE stream.

        Bounded: the stream is opened with `timeout=idle`, which curl_cffi
        maps onto CURLOPT_LOW_SPEED_TIME (1 B/s) — a silent stream is killed
        after `idle` seconds and surfaces as asyncio.TimeoutError, so the
        caller's wait_for/cancellation always lands and nothing hangs.
        Cancellation-safe: the underlying curl session is closed in a finally.

        On idle — or on a mid-stream blip (ProviderDown) — the loop
        reconnects, which is exactly what the site's own data-init re-patch
        does. Reconnects are paused so a dead endpoint never becomes a hot
        loop, and after `MAX_RECONNECTS` consecutive failed connects the
        ProviderDown propagates: a persistent outage must surface, not spin
        forever.
        """
        idle = self.caps.poll_interval if poll is None else max(poll, 0.001)
        seen: set[str] = set()
        failures = 0
        while True:
            try:
                async for patch in self._frames(address, idle):
                    failures = 0
                    for row in _rows(patch.elements):
                        if row.id in seen:
                            continue
                        seen.add(row.id)
                        try:
                            yield await self.get(address, row.id)
                        except MessageGone:
                            continue
            except TimeoutError:
                failures = 0
            except ProviderDown:
                failures += 1
                if failures > MAX_RECONNECTS:
                    raise
            await asyncio.sleep(min(idle, 5.0))

    async def list(self, address: Address) -> list[MessageRow]:
        """Bounded collect: one watch connection, rows from the frames it
        sends before the idle bound. The first frame after connect carries the
        whole current page of the inbox (all rows, or the empty re-patch), so
        an empty inbox is [] after the idle wait, never an exception. Only the
        idle timeout is swallowed — a connect failure or dropped stream is a
        ProviderDown the caller must see, not an inbox that looks empty."""
        rows: dict[str, MessageRow] = {}
        try:
            async for patch in self._frames(address, idle=3.0):
                for row in _rows(patch.elements):
                    rows[row.id] = row
                if rows:
                    break
        except TimeoutError:
            pass
        return list(rows.values())

    async def get(self, address: Address, id: str) -> Message:
        """One message: the mta host answers a GET on
        /app/message/<addr>/<id>?unsafe=true with a single SSE patch carrying
        the header block and the body iframe (captured live). A 404 is an
        expired or deleted message; the endpoint 500s without the datastar
        signals param, which is request shaping, not drift."""
        url = (
            f"{MTA}/app/message/{address.value}/{id}?unsafe=true"
            f"&datastar={quote(json.dumps(SIGNALS))}"
        )
        resp = await self.http.send("GET", url, headers=SSE_HEADERS)
        if resp.status_code == 404:
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code >= 400:
            raise ProviderDown(f"{self.name}: message read failed, HTTP {resp.status_code}")
        patches = parse_sse(resp.text)
        if not patches or not patches[0].elements:
            raise SchemaDrift(self.name, expected="a message body patch", got=resp.text[:200])
        return _message_from_patch(patches[0], id)

    async def delete(self, address: Address, id: str) -> None:
        """DELETE /app/message/<addr>/<id> on the mta host — the exact call
        the row's trash button makes (`data-on:click="@delete('.../<id>')"`,
        captured live). No state needed: the address in the path authorizes."""
        resp = await self.http.send(
            "DELETE", f"{MTA}/app/message/{address.value}/{id}", headers=SSE_HEADERS
        )
        if resp.status_code == 404:
            # Already gone: the caller wanted the message gone, and it is.
            return
        if resp.status_code >= 400:
            raise ProviderDown(f"{self.name}: delete failed, HTTP {resp.status_code}")
