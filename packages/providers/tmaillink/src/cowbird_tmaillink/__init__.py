"""tmail.link — a self-hosted Django disposable-mail app, one domain, no gate.

Three plain endpoints, traced and proved live 2026-11-15 (survey E):

- `GET /` server-renders a random `first.last@tmail.link` address; any custom
  local part also works — `GET /inbox/<anything>@tmail.link/` creates and
  serves the inbox.
- `POST /inbox/<addr>/` with form body `format=json&csrfmiddlewaretoken=<the
  csrftoken cookie>` lists the inbox as `{"messages": [...]}`. The CSRF token
  is the cookie value Django issued to any visitor; without it the POST is a
  plain Django 403. `GET …?format=json` does NOT work (HTML).
- `GET /inbox/<addr>/<key>/` renders the message; the body itself sits in an
  iframe at `<key>/1/`. An unknown key is a bare Django 404 — MessageGone.

The inbox is PUBLIC: anyone with the address (plus a csrftoken cookie Django
hands to any visitor) reads it. No credentials, no captcha, no state that can
expire — but the csrftoken travels in Address.state so an out-of-process
caller does not have to re-open the page for it.
"""

from __future__ import annotations

import json
import re
import string
from datetime import datetime, timedelta
from typing import ClassVar

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://tmail.link"
# The message page's own copy: "(messages are deleted automatically after 24 hours)"
MESSAGE_TTL = timedelta(hours=24)
_LOCAL_ALPHABET = string.ascii_lowercase + string.digits

_HOME_ADDR = re.compile(r"([A-Za-z0-9._+-]+@tmail\.link)")
_IFRAME_SRC = re.compile(r'<iframe[^>]*\ssrc="([^"]*)"')
_PRE = re.compile(r"<pre>(.*)</pre>", re.DOTALL)
_TABLE_ROW = re.compile(
    r"<tr><td><b>(To|From|Subject|Date|Message-Id)</b>:</td><td>(.*?)</td></tr>"
)
_CSRF_INPUT = re.compile(r"csrfmiddlewaretoken' value='([^']+)")
_UNESCAPE = {"&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&#39;": "'"}


def _unescape(text: str) -> str:
    for entity, char in _UNESCAPE.items():
        text = text.replace(entity, char)
    return text


def _field(headers: dict[str, str], key: str) -> str:
    if key not in headers:
        raise SchemaDrift(
            "tmaillink", expected=f"a {key} row on the message page", got=list(headers)
        )
    return headers[key]


def _csrf(state: str | None) -> str:
    if not state:
        return ""
    try:
        return json.loads(state)["csrftoken"]
    except (KeyError, TypeError, ValueError) as exc:
        raise NotSupported("tmaillink needs the state issued by generate()") from exc


class TMailLink(Provider):
    name = "tmaillink"
    api: ClassVar[str] = SITE
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("tmail.link",),
        domains=("tmail.link",),
        domain_count=1,
        address_ttl=None,  # custom inboxes persisted server-side across the whole probe day
        message_ttl=MESSAGE_TTL,
        push=False,  # the page polls every 5 s client-side
        delete=True,
        custom_local=True,
        self_hosted=True,
        needs_residential_ip=False,
        needs_state=False,  # a fresh visitor cookie is enough; state only saves a round trip
        inbox_privacy=InboxPrivacy.PUBLIC,
    )

    async def _token(self, address: Address) -> str:
        """The csrftoken from state, or a fresh one from opening the inbox."""
        token = _csrf(address.state)
        if token:
            return token
        resp = await self.http.send("GET", f"{self.api}/inbox/{address.value}/")
        token = resp.cookies.get("csrftoken", "")
        if not token:
            raise SchemaDrift(
                self.name, expected="a csrftoken cookie on the inbox page", got=dict(resp.cookies)
            )
        return token

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.kind is not None and opts.kind is not Kind.OWN_DOMAIN:
            raise NotSupported(f"tmaillink does not serve {opts.kind}")
        if opts.domain and opts.domain not in self.caps.domains:
            raise NotSupported(
                f"tmaillink does not serve {opts.domain}; has {list(self.caps.domains)}"
            )
        if opts.local:
            value = f"{opts.local}@tmail.link"
        else:
            home = await self.http.text("GET", f"{self.api}/")
            m = _HOME_ADDR.search(home)
            if not m:
                raise SchemaDrift(self.name, expected="an address on the homepage", got=home[:200])
            value = m.group(1)
        # Opening the inbox creates it and captures the csrftoken into state.
        resp = await self.http.send("GET", f"{self.api}/inbox/{value}/")
        token = resp.cookies.get("csrftoken", "")
        if not token:
            raise SchemaDrift(
                self.name, expected="a csrftoken cookie on the inbox page", got=dict(resp.cookies)
            )
        return Address(value=value, provider=self.name, state=json.dumps({"csrftoken": token}))

    async def list(self, address: Address) -> list[MessageRow]:
        token = await self._token(address)
        payload = await self.http.json(
            "POST",
            f"{self.api}/inbox/{address.value}/",
            data={"format": "json", "csrfmiddlewaretoken": token},
            headers={"Referer": f"{self.api}/inbox/{address.value}/"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise SchemaDrift(
                self.name,
                expected='{"messages": [...]} from the inbox POST',
                got=str(payload)[:200],
            )
        rows = []
        for row in payload["messages"]:
            if not isinstance(row, dict) or "key" not in row:
                raise SchemaDrift(
                    self.name, expected="'key' in every message row", got=str(row)[:200]
                )
            received = None
            raw = row.get("created")
            if raw:
                try:
                    received = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    received = None
            rows.append(
                MessageRow(
                    id=row["key"],
                    sender=row.get("sender", ""),
                    subject=row.get("subject", ""),
                    received_at=received,
                )
            )
        return rows

    async def get(self, address: Address, id: str) -> Message:
        resp = await self.http.send(
            "GET", f"{self.api}/inbox/{address.value}/{id}/", allow_status=(404,)
        )
        if resp.status_code == 404:
            raise MessageGone(f"tmaillink: message {id} is gone")
        iframe = _IFRAME_SRC.search(resp.text)
        headers = {
            label: _unescape(value.strip()) for label, value in _TABLE_ROW.findall(resp.text)
        }
        if iframe:
            src = iframe.group(1)
            if not src.endswith("/"):
                src += "/"  # the page's src="1" resolves relative to the message URL
            body = await self.http.text("GET", f"{self.api}/inbox/{address.value}/{id}/{src}")
        else:
            # A plain-text message renders inline in a <pre> instead of the
            # HTML iframe; that pre IS the body.
            m = _PRE.search(resp.text)
            if not m:
                raise SchemaDrift(
                    self.name,
                    expected="a body iframe or a plain-text pre on the message page",
                    got=resp.text[:200],
                )
            body = m.group(1)
        received = None
        raw_date = headers.get("Date")
        if raw_date:
            from email.utils import parsedate_to_datetime

            try:
                received = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                received = None
        return Message(
            id=id,
            sender=_field(headers, "From"),
            subject=_field(headers, "Subject"),
            received_at=received,
            html=body,
            text=html_to_text(body),
            links=extract_links(body),
        )

    async def delete(self, address: Address, id: str) -> None:
        """The message page's own form: POST `delete=1` back to its URL."""
        resp = await self.http.send(
            "GET", f"{self.api}/inbox/{address.value}/{id}/", allow_status=(404,)
        )
        if resp.status_code == 404:
            return  # already gone; delete is idempotent
        m = _CSRF_INPUT.search(resp.text)
        if not m:
            raise SchemaDrift(
                self.name, expected="a csrfmiddlewaretoken on the message page", got=resp.text[:200]
            )
        done = await self.http.send(
            "POST",
            f"{self.api}/inbox/{address.value}/{id}/",
            data={"csrfmiddlewaretoken": m.group(1), "delete": "1"},
            headers={"Referer": f"{self.api}/inbox/{address.value}/{id}/"},
        )
        if done.status_code >= 400:
            raise ProviderDown(f"{self.name}: delete failed, HTTP {done.status_code}")
