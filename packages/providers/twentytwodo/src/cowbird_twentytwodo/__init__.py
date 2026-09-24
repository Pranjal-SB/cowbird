"""22.do — Gmail, Outlook and own-domain addresses from one backend.

The server picks the kind at random whatever `type` is sent, so asking for one
kind means re-rolling. The message body is not in any JSON response: the
content page embeds it in an iframe served from /view/<token>.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime, timedelta

from cowbird.errors import AddressExpired, MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

API = "https://22.do"
# gmail came back 2 times in 5 samples. Six tries fail about 1 time in
# 50 at that rate; raise the bound, or pick from the homepage's prefilled
# address, if gmail gets rarer.
REROLLS = 6
_KINDS = {"gmail": Kind.GMAIL_ALIAS, "microsoft": Kind.OUTLOOK_ALIAS, "domain": Kind.OWN_DOMAIN}
_IFRAME = re.compile(r'<iframe[^>]*\bsrc="([^"]*/view/[^"]+)"', re.IGNORECASE)


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(row["time"]), UTC)
    except (KeyError, TypeError, ValueError, OSError):
        return None


class TwentyTwoDo(Provider):
    name = "22do"
    caps = Capabilities(
        kind=frozenset({Kind.GMAIL_ALIAS, Kind.OUTLOOK_ALIAS, Kind.OWN_DOMAIN}),
        sites=("22.do",),
        domains=(),
        domain_count=3,
        address_ttl=timedelta(days=1),
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
    )

    def _auth(self, address: Address) -> dict[str, str]:
        if not address.state:
            raise NotSupported("22do needs the token issued by generate()")
        return {"Authorization": f"Bearer {address.state}"}

    async def _post(self, path: str, body: dict, headers: dict | None = None) -> object:
        resp = await self.http.send("POST", f"{API}{path}", json=body, headers=headers or {})
        if resp.status_code >= 400:
            raise ProviderDown(f"22do: {path} answered HTTP {resp.status_code}")
        try:
            payload = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        if not isinstance(payload, dict) or "status" not in payload:
            raise SchemaDrift(self.name, expected=f"'status' from {path}", got=payload)
        if payload["status"] is not True:
            raise ProviderDown(f"22do: {path} refused: {payload.get('msg')!r}")
        return payload.get("data")

    async def _post_with_token(self, path: str, body: dict, address: Address) -> object:
        """POST with token, checking status before parsing JSON to avoid SchemaDrift on 401."""
        resp = await self.http.send(
            "POST", f"{API}{path}", json=body, headers=self._auth(address)
        )
        if resp.status_code == 401:
            raise AddressExpired(f"22do: {address.value} is no longer valid")
        if resp.status_code >= 400:
            raise ProviderDown(f"22do: {path} answered HTTP {resp.status_code}")
        try:
            payload = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        if not isinstance(payload, dict) or "status" not in payload:
            raise SchemaDrift(self.name, expected=f"'status' from {path}", got=payload)
        if payload["status"] is not True:
            raise ProviderDown(f"22do: {path} refused: {payload.get('msg')!r}")
        return payload.get("data")

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        want = opts.kind if opts else None
        for _ in range(REROLLS):
            data = await self._post("/action/mailbox/create", {"type": "random"})
            if not isinstance(data, dict) or not {"email", "type"} <= data.keys():
                raise SchemaDrift(self.name, expected="'email' and 'type' from create", got=data)
            if want is None or _KINDS.get(data["type"]) is want:
                break
        else:
            raise ProviderDown(f"22do: no {want} address in {REROLLS} tries")
        token = await self._post(
            "/action/mailbox/applyToken", {"email": data["email"], "uuid": secrets.token_hex(16)}
        )
        if not isinstance(token, dict) or "token" not in token:
            raise SchemaDrift(self.name, expected="'token' from applyToken", got=token)
        return Address(
            value=data["email"],
            provider=self.name,
            expires_at=datetime.now(UTC) + self.caps.address_ttl,
            state=token["token"],
        )

    async def list(self, address: Address) -> list[MessageRow]:
        data = await self._post_with_token(
            "/action/mailbox/message",
            {"email": address.value, "lastime": 0},
            address,
        )
        # An empty inbox is null, except an empty Outlook one, which is false.
        if data is None or data is False:
            return []
        if not isinstance(data, list):
            raise SchemaDrift(self.name, expected="a list or null under 'data'", got=data)
        out = []
        for row in data:
            if not isinstance(row, dict) or "messageId" not in row:
                got = list(row) if isinstance(row, dict) else type(row).__name__
                raise SchemaDrift(self.name, expected="'messageId' in a message row", got=got)
            out.append(
                MessageRow(
                    id=row["messageId"],
                    sender=row.get("from", ""),
                    subject=row.get("subject", ""),
                    received_at=_at(row),
                )
            )
        return out

    async def get(self, address: Address, id: str) -> Message:
        page = await self.http.send("GET", f"{API}/content/{id}", headers=self._auth(address))
        if page.status_code == 401:
            raise AddressExpired(f"22do: {address.value} is no longer valid")
        if page.status_code == 404:
            raise MessageGone(f"22do: message {id} is gone")
        if page.status_code >= 400:
            raise ProviderDown(f"22do: content page answered HTTP {page.status_code}")
        match = _IFRAME.search(page.text)
        if match is None:
            raise SchemaDrift(self.name, expected="an iframe to /view/ on the content page",
                              got=page.text[:200])
        view = await self.http.send("GET", match.group(1), headers=self._auth(address))
        if view.status_code == 401:
            raise AddressExpired(f"22do: {address.value} is no longer valid")
        if view.status_code >= 400:
            raise ProviderDown(f"22do: view page answered HTTP {view.status_code}")
        html = view.text
        row = next((r for r in await self.list(address) if r.id == id), None)
        return Message(
            id=id,
            sender=row.sender if row else "",
            subject=row.subject if row else "",
            received_at=row.received_at if row else None,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
