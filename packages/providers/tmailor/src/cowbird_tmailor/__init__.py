"""tmailor.com — JSON POST API behind a per-mailbox JWT.

The survey's Datastar premise was false: the site is a plain JSON POST API
(`webapp-*` endpoints) plus a WebSocket push channel (`wss://wss.tmailor.com`
via a SharedWorker). Push is out of scope here — the polling default covers
it, and `caps.push` is False.

The load-bearing quirk is the list shape: `webapp-listinbox` requires a
`listToken` MAP (`{email: accesstoken}`, up to 10 mailboxes); the single-token
variant (`{"curentToken": jwt}`) answers `{"msg":"errortoken"}`. Verified live
2026-11-15.

Read/delete key a message by its `email_id` blob (an opaque base64 string),
not by the uuid in `id`/`uuid` — passing the uuid answers `errortoken`. The
`email_code` needed by read/delete is a per-listinbox-call uuid that changes
between calls, so it is always taken from the same list response the call is
derived from, never cached.

Domains: the only client-visible domain list is the homepage `<select>` for
`choose_domain`, which (observed 2026-11-15) offers a single option,
value 1 = "Random domain". The adapter scrapes it anyway so a future option
list is honored, but names a domain the select does not offer is refused.
The server issues random local parts; the site's custom-name UI flow exists
(`custom_name` in the JS) but is a captcha-gated path we do not implement.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import ClassVar

from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, InboxPrivacy, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://tmailor.com"
API = f"{SITE}/api"


def _field(row: dict, key: str, where: str) -> str:
    """A missing required field is schema drift, not a bare KeyError."""
    if key not in row:
        raise SchemaDrift("tmailor", expected=f"{key!r} in {where}", got=list(row))
    return row[key]


# Answers that mean this egress is blocked for a while; recon saw one last
# about ten minutes. The adapter is fine, so they must not quarantine it.
_BLOCKED = frozenset({"errorcaptcha", "client-block", "cfblock"})


def _ok(payload: object, where: str) -> dict:
    """Every webapp-* answer is `{"msg": ...}`. A block is an outage, a
    rejected mailbox token is a dead address, and anything else that is not
    "ok" is a shape this adapter does not know."""
    msg = payload.get("msg") if isinstance(payload, dict) else None
    if msg == "ok":
        return payload
    if msg in _BLOCKED:
        raise ProviderDown(f"tmailor: {msg} from {where}")
    if msg == "errortoken":
        raise AddressExpired(f"tmailor: errortoken from {where}")
    got = payload if isinstance(payload, dict) else type(payload).__name__
    raise SchemaDrift("tmailor", expected=f'msg "ok" from {where}', got=got)


def _box(payload: dict, address: str, where: str) -> dict:
    """The per-mailbox entry of a listinbox answer: {code, data}."""
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(address), dict):
        raise SchemaDrift(
            "tmailor", expected=f'data[{address!r}] from {where}', got=data
        )
    return data[address]


def _rows(box: dict, where: str) -> list[dict]:
    """`data` is null on an empty inbox, else a map keyed by message uuid."""
    data = box.get("data")
    if data is None:
        return []
    if not isinstance(data, dict):
        raise SchemaDrift("tmailor", expected="a message map or null", got=data)
    return list(data.values())


_OPTION = re.compile(r'<option value="([^"]+)"[^>]*>([^<]*)</option>')


def _domain_options(html: str) -> dict[str, str]:
    """The choose_domain select: value → label. Only non-"1" entries name a
    real domain; value 1 is the server's random pool."""
    return {
        value.strip(): label.strip()
        for value, label in _OPTION.findall(html)
        if value.strip() not in ("", "1")
    }


class Tmailor(Provider):
    name = "tmailor"
    api: ClassVar[str] = API
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN}),
        sites=("tmailor.com",),
        domains=(),  # rotating server-side pool; the client select ships none
        domain_count=0,
        address_ttl=None,
        message_ttl=None,  # site copy claims long retention; no number shipped
        push=False,  # wss://wss.tmailor.com exists (SharedWorker); out of scope
        delete=True,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,  # the per-mailbox JWT travels in Address.state
        inbox_privacy=InboxPrivacy.PRIVATE,  # every read is JWT-gated
    )

    async def _homepage(self) -> str:
        return await self.http.text("GET", f"{SITE}/")

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.local:
            raise NotSupported(
                f"{self.name} issues server-random local parts only; "
                "the site's custom-name flow is a captcha-gated UI path"
            )
        choose_domain = 1
        if opts.domain:
            offered = _domain_options(await self._homepage())
            # value 1 is "Random domain"; any named option must match exactly.
            match = [v for v, label in offered.items() if label == opts.domain]
            if not match:
                raise NotSupported(
                    f"{self.name} does not serve {opts.domain}; "
                    f"the choose_domain select offers {sorted(offered.values())} "
                    "plus random (value 1)"
                )
            if not match[0].isdigit():
                # The select's values are numeric flags to the backend; a
                # non-numeric value means the scrape matched something else
                # (drift), not a domain we can request.
                raise SchemaDrift(
                    "tmailor",
                    expected="numeric choose_domain option values",
                    got=match[0],
                )
            choose_domain = int(match[0])
        payload = _ok(
            await self.http.json(
                "POST",
                f"{self.api}/webapp-newemail",
                json={"action": "newemail", "curentToken": "", "choose_domain": choose_domain},
            ),
            "webapp-newemail",
        )
        value = _field(payload, "email", "webapp-newemail")
        token = _field(payload, "accesstoken", "webapp-newemail")
        return Address(
            value=value,
            provider=self.name,
            state=json.dumps({"address": value, "token": token}),
        )

    def _token(self, address: Address) -> str:
        if not address.state:
            raise NotSupported("tmailor needs the state issued by generate()")
        return json.loads(address.state)["token"]

    async def _inbox_page(self, address: Address) -> dict:
        """One listinbox round trip; the map shape is load-bearing."""
        payload = _ok(
            await self.http.json(
                "POST",
                f"{self.api}/webapp-listinbox",
                json={
                    "action": "listinbox",
                    "listToken": {address.value: self._token(address)},
                },
            ),
            "webapp-listinbox",
        )
        return _box(payload, address.value, "webapp-listinbox")

    async def list(self, address: Address) -> list[MessageRow]:
        box = await self._inbox_page(address)
        return [
            MessageRow(
                # Callers and get() key on the email_id blob; the uuid in `id`
                # is rejected by webapp-read.
                id=_field(row, "email_id", "a listinbox row"),
                sender=row.get("sender_email") or row.get("sender_name") or "",
                subject=row.get("subject") or "",
                received_at=(
                    datetime.fromtimestamp(row["receive_time"], tz=UTC)
                    if row.get("receive_time")
                    else None
                ),
            )
            for row in _rows(box, "webapp-listinbox")
        ]

    async def get(self, address: Address, id: str) -> Message:
        # email_code changes between list calls, so resolve it fresh and read
        # in the same breath. The same page tells us whether the message still
        # exists: if its email_id is no longer listed, it expired or was
        # deleted between list and get — MessageGone, so a watch() stream
        # skips it instead of dying on SchemaDrift. Caveat: not re-verified
        # live post-fix — after the fix-round egress drew a persistent
        # `errorcaptcha`/client-block on webapp-newemail (three attempts over
        # ~10 min). What IS verified live: a listed message reads fine, and a
        # listed-then-deleted message disappears from listinbox (the proof
        # run's post-delete list came back empty), so the listed-set is the
        # best available existence oracle.
        page = await self._inbox_page(address)
        code = _field(page, "code", "webapp-listinbox")
        if id not in {r.get("email_id") for r in _rows(page, "webapp-listinbox")}:
            raise MessageGone(f"{self.name}: message {id} is gone")
        payload = _ok(
            await self.http.json(
                "POST",
                f"{self.api}/webapp-read",
                json={
                    "action": "read",
                    "accesstoken": self._token(address),
                    "email_code": code,
                    "email_token": id,
                },
            ),
            "webapp-read",
        )
        row = payload.get("data")
        if not isinstance(row, dict):
            raise SchemaDrift("tmailor", expected="data in webapp-read", got=row)
        html = row.get("url_body") or ""
        text = row.get("body") or ""
        return Message(
            # Echo the email_id blob we were called with, not the uuid the read
            # doc carries in `id`: delete() and Inbox cleanup key on the blob,
            # and callers pass row.id straight back to delete().
            id=id,
            sender=row.get("sender_email") or row.get("sender_name") or "",
            subject=row.get("subject") or "",
            received_at=(
                datetime.fromtimestamp(row["receive_time"], tz=UTC)
                if row.get("receive_time")
                else None
            ),
            html=html,
            text=text or html_to_text(html),
            links=extract_links(html),
        )

    async def delete(self, address: Address, id: str) -> None:
        code = _field(await self._inbox_page(address), "code", "webapp-listinbox")
        # A non-ok answer (errortoken, cfblock) means the delete did not happen;
        # surface it rather than pretending the message is gone.
        _ok(
            await self.http.json(
                "POST",
                f"{self.api}/webapp-delete",
                json={
                    "action": "delete",
                    "accesstoken": self._token(address),
                    "email_code": code,
                    "email_token": id,
                },
            ),
            "webapp-delete",
        )
