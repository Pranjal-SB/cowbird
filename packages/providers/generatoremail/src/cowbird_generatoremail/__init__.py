"""emailfake.com — the generator-email family (generator.email, emailfake.com,
tempm.com, mail-temp.com: one backend on mx.add5000.com).

An inbox is any local part on any served domain. The mailbox page is keyed by
the URL alone (`/<local>@<domain>`), so anyone with the address can read it —
no cookies, no credentials (InboxPrivacy.PUBLIC).

The backend fronts the site with a proof-of-work gate. A request that does not
look like a browser gets a 503 (`x-ab-interstitial: 1`) whose body carries a
fresh salt `C`; the client must find n with sha256(C + ":" + n) having 15
leading zero bits and present it to `GET /__verify?c=&n=&t=&b=&z=`. A cleared
browser-like client gets the HttpOnly `__Host-ab_hp` cookie, good 45 minutes
(both timestamps are embedded in the cookie value).

Under the runtime's chrome impersonation the gate does not engage (observed
2026-11-15: 200 on every request, 10/10 rapid-fire included) — but the gate is
real (observed from this infrastructure in September 2026) and egress-dependent,
so the full flow is implemented: interstitial → solve → /__verify → capture the
cookie → replay it from Address.state on every later call, because the
transport's fresh sessions drop it. One caveat, measured live: the interstitial
mimics Cloudflare (`<title>Just a moment...</title>`), so Transport classifies
it as a challenge and raises CloudflareChallenge before `allow_status=(503,)`
can hand the body over. See the task 6 report for the full matrix.

Domains are scraped from /api/domains.php, which serves a rotating 15-domain
slice of a much larger pool per call (52 distinct observed across four calls
seconds apart).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import string
from datetime import UTC, datetime
from typing import ClassVar

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://emailfake.com"
VERIFY = f"{SITE}/__verify"
VERIFY_COOKIE = "__Host-ab_hp"
POW_CAP = 2**22  # the page's own budget is 500k; ours is the brief's 2**22
_ZERO_BITS = 15
# The page's own fetch(): the domains API refuses cross-site-looking calls.
_FETCH = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
_LOCAL_ALPHABET = string.ascii_lowercase + string.digits

_SALT = re.compile(r'var C="([^"]*)"')
_API_TOKEN = re.compile(r'<meta name="api-token" content="([^"]*)"')
_PAGE_MID = re.compile(r'data-mid="([0-9a-f]{16,})"')
_ROW = re.compile(
    r"loadInboxClientSide\('(?P<ctx>[^']*)'\)\"\ class=\"[^\"]*\">"
    r'<div class="e7m from_div_45g45gg">(?P<sender>.*?)</div>'
    r'<div class="e7m subj_div_45g45gg">(?P<subject>.*?)</div>'
    r'<div class="e7m time_div_45g45gg">(?P<time>.*?)</div>'
)
_SUMMARY_ROW = re.compile(
    r'<div id="mail-summary-head"[^>]*>'
    r'<div class="e7m from_div_45g45gg">(?P<sender>.*?)</div>'
    r'<div class="e7m subj_div_45g45gg">(?P<subject>.*?)</div>'
    r'<div class="e7m time_div_45g45gg">(?P<time>.*?)</div>'
)


def _solve_pow(salt: str, cap: int = POW_CAP) -> int:
    """The first n with sha256(salt + ":" + n) having 15 leading zero bits.

    Pure so the fixture pair in the tests pins the exact algorithm: a known
    salt must yield the precomputed nonce, and exhausting the cap means the
    backend raised the difficulty or changed the formula — ProviderDown, the
    pool should route around it.
    """
    prefix = f"{salt}:".encode()
    for n in range(cap):
        digest = hashlib.sha256(prefix + b"%d" % n).digest()
        if int.from_bytes(digest[:2], "big") >> (16 - _ZERO_BITS) == 0:
            return n
    raise ProviderDown(f"generatoremail: proof of work unsolved after {cap} iterations")


def _pack(cookie: str) -> str:
    """The verified-session cookie, for the caller to carry across processes."""
    return json.dumps({"ab_hp": cookie})


def _cookies(state: str | None) -> dict[str, str]:
    if not state:
        return {}
    try:
        cookie = json.loads(state)["ab_hp"]
    except (KeyError, TypeError, ValueError) as exc:
        raise NotSupported("generatoremail needs the state issued by generate()") from exc
    return {VERIFY_COOKIE: cookie} if cookie else {}


def _at(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _inner(html: str, marker: str) -> str:
    """The inner HTML of the first <div> whose tag contains `marker`, by
    balanced-div scan — the body block holds arbitrary sender HTML, so a
    closing-tag regex would stop at the first nested `</div>`."""
    anchor = html.find(marker)
    if anchor < 0:
        raise SchemaDrift("generatoremail", expected=marker, got=html[:200])
    open_end = html.find(">", anchor)
    if open_end < 0:
        raise SchemaDrift("generatoremail", expected=f"{marker} to open a tag", got=html[:200])
    start = open_end + 1
    depth, i = 1, start
    while depth:
        o, c = html.find("<div", i), html.find("</div>", i)
        if c < 0:
            raise SchemaDrift(
                "generatoremail", expected=f"{marker} to close", got=html[start : start + 200]
            )
        if 0 <= o < c:
            depth += 1
            i = o + 4
        else:
            depth -= 1
            i = c + 6
    return html[start : i - 6]


class GeneratorEmail(Provider):
    name = "generatoremail"
    api: ClassVar[str] = SITE
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("emailfake.com",),
        domains=(),  # a rotating 15-domain slice, scraped per generate()
        domain_count=52,  # distinct domains observed across four scrapes, 2026-11-15
        address_ttl=None,
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        fresh_session=True,
        inbox_privacy=InboxPrivacy.PUBLIC,
    )

    def __init__(self, http) -> None:
        super().__init__(http)
        self._token: str | None = None

    async def _get(self, path: str, state: str | None = None, headers: dict | None = None):
        """GET with the gate handled: a 503 interstitial is solved once and the
        request retried with the verified cookie. Still gated after that is
        ProviderDown — one solve per call, no loop.

        Returns (response, cookie): the `__Host-ab_hp` value this call captured,
        empty when the gate never engaged. list()/get() discard it (an Address
        is frozen); generate() packs it into the state it issues."""
        cookies = _cookies(state)
        merged = {**_FETCH, **(headers or {})}
        resp = await self.http.send(
            "GET", f"{SITE}{path}", allow_status=(503,), cookies=cookies or None, headers=merged
        )
        if resp.status_code != 503:
            return resp, ""
        cookie = await self._verify_gate(resp)
        if cookie:
            cookies = {**cookies, VERIFY_COOKIE: cookie}
        resp = await self.http.send(
            "GET", f"{SITE}{path}", allow_status=(503,), cookies=cookies or None, headers=merged
        )
        if resp.status_code == 503:
            raise ProviderDown(f"{self.name}: still gated after the proof of work")
        return resp, cookie

    async def _verify_gate(self, resp) -> str:
        salt = _SALT.search(resp.text)
        if not salt:
            raise SchemaDrift(
                self.name, expected="a PoW salt in the interstitial", got=resp.text[:200]
            )
        n = _solve_pow(salt.group(1))
        # b=0/z=0: a clean browser fingerprint and UTC, accepted live 2026-11-15.
        verified = await self.http.send(
            "GET",
            VERIFY,
            params={"c": salt.group(1), "n": n, "t": "/", "b": "0", "z": "0"},
            allow_status=(503,),
        )
        if verified.status_code != 200:
            raise ProviderDown(f"{self.name}: /__verify answered HTTP {verified.status_code}")
        return verified.cookies.get(VERIFY_COOKIE, "")

    async def _api_token(self) -> str:
        if not self._token:
            resp, _ = await self._get("/")
            m = _API_TOKEN.search(resp.text)
            if not m:
                raise SchemaDrift(
                    self.name, expected="an api-token meta on the page", got=resp.text[:200]
                )
            self._token = m.group(1)
        return self._token

    async def _domains(self) -> list[str]:
        resp = None
        for attempt in range(2):
            resp, _ = await self._get(
                "/api/domains.php", headers={"X-API-Token": await self._api_token()}
            )
            if resp.status_code == 200:
                break
            if resp.status_code == 403 and attempt == 0:
                self._token = None  # the token is time-bound; re-scrape it
            else:
                raise ProviderDown(f"{self.name}: domains API answered HTTP {resp.status_code}")
        try:
            rows = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(
                self.name, expected="a JSON domain list", got=resp.text[:200]
            ) from exc
        domains = sorted({r["ascii"] for r in rows if isinstance(r, dict) and r.get("ascii")})
        if not domains:
            raise SchemaDrift(self.name, expected="domains with 'ascii'", got=resp.text[:200])
        return domains

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.kind is not None and opts.kind is not Kind.OWN_DOMAIN:
            raise NotSupported(f"generatoremail does not serve {opts.kind}")
        domains = await self._domains()
        if opts.domain and opts.domain not in domains:
            raise NotSupported(f"generatoremail does not serve {opts.domain}; has {domains}")
        domain = opts.domain or secrets.choice(domains)
        local = opts.local or "".join(secrets.choice(_LOCAL_ALPHABET) for _ in range(10))
        value = f"{local}@{domain}"
        # Opening the mailbox once proves the address resolves and captures the
        # gate cookie into state; fresh sessions would otherwise drop it.
        _, cookie = await self._get(f"/{value}")
        return Address(value=value, provider=self.name, state=_pack(cookie))

    async def list(self, address: Address) -> list[MessageRow]:
        resp, _ = await self._get(f"/{address.value}", address.state)
        table = _inner(resp.text, 'id="email-table"')
        rows = [
            MessageRow(
                id=m["ctx"].rsplit("/", 1)[-1],
                sender=m["sender"],
                subject=m["subject"],
                received_at=_at(m["time"]),
            )
            for m in _ROW.finditer(table)
        ]
        if not rows:
            # Exactly one message renders as a summary head without the
            # onclick; the id then comes from the page's own data-mid.
            m = _SUMMARY_ROW.search(table)
            page_mid = _PAGE_MID.search(resp.text)
            if m and page_mid:
                rows.append(
                    MessageRow(
                        id=page_mid.group(1),
                        sender=m["sender"],
                        subject=m["subject"],
                        received_at=_at(m["time"]),
                    )
                )
        return rows

    async def get(self, address: Address, id: str) -> Message:
        local, domain = address.value.split("@", 1)
        resp, _ = await self._get(f"/{domain}/{local}/{id}", address.state)
        page_mid = _PAGE_MID.search(resp.text)
        if page_mid is None or "mess_bodiyy" not in resp.text:
            # An unknown mid answers the mailbox page: no data-mid, no body.
            raise MessageGone(f"generatoremail: message {id} is gone")
        if page_mid.group(1) != id:
            # An expired mid on a busy mailbox answers the message shown there
            # now. The one asked for is gone; the page itself is fine.
            raise MessageGone(f"generatoremail: message {id} is gone")
        head = _SUMMARY_ROW.search(_inner(resp.text, 'id="email-table"'))
        if not head:
            raise SchemaDrift(
                self.name, expected="a mail-summary-head on the message page", got=resp.text[:200]
            )
        html = _inner(resp.text, "mess_bodiyy")
        return Message(
            id=id,
            sender=head["sender"],
            subject=head["subject"],
            received_at=_at(head["time"]),
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
