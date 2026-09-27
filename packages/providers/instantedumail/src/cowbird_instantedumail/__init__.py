"""instantedumail.com, a Supabase app used through its own REST endpoints.

An anonymous Supabase sign-up issues an access token and a refresh token.
The client then inserts a mailbox row whose address it picks itself, and
reads the `emails` table filtered by that mailbox. Rows carry the body, text
and HTML both, so get() is a single-row read. Row-level security holds:
a token reads only its own user's mailboxes, so an inbox is private.

None of the domains is a university's: mail-edu.eu and the .io.vn names are
ordinary registrations, and myacademy.edu.pl is under .edu.pl, which anyone
can register.

The key below is the project's publishable anon key, shipped in the page
bundle. It is not a secret. The tokens in Address.state are.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cowbird.errors import MessageGone, NotSupported, ProviderDown, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

PROJECT = "https://qapwrbodvcsseymwmmxw.supabase.co"
APIKEY = "sb_publishable_k4cc22sdcI4IOUqlTHo3uA_yXyaJupH"
# The domains the page marks live.
DOMAINS = ("mail-edu.eu", "myacademy.edu.pl", "manila.io.vn", "chicago.io.vn")
LIFETIME = timedelta(hours=1)


def _at(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except ValueError:
        return None


def _sender(row: dict) -> str:
    name, address = row.get("from_name") or "", row.get("from_address") or ""
    return f"{name} <{address}>" if name and address else address or name


class InstantEduMail(Provider):
    name = "instantedumail"
    caps = Capabilities(
        kind=frozenset({Kind.OWN_DOMAIN, Kind.EDU}),
        sites=("instantedumail.com",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        # The client asks for an hour; the server has been seen to grant two.
        address_ttl=LIFETIME,
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=True,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
    )

    def __init__(self, http) -> None:
        super().__init__(http)
        # Refreshed tokens, by address. Address is immutable, so a refresh is
        # remembered here for the life of this provider.
        self._tokens: dict[str, dict[str, str]] = {}

    def _headers(self, token: str | None = None) -> dict[str, str]:
        return {"apikey": APIKEY, "Authorization": f"Bearer {token or APIKEY}"}

    def _state(self, address: Address) -> dict[str, str]:
        if address.value in self._tokens:
            return self._tokens[address.value]
        try:
            state = json.loads(address.state or "")
            return {k: state[k] for k in ("access_token", "refresh_token", "mailbox_id")}
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported(f"{self.name} needs the tokens issued by generate()") from exc

    async def _refresh(self, address: Address, state: dict[str, str]) -> dict[str, str]:
        resp = await self.http.send(
            "POST",
            f"{PROJECT}/auth/v1/token?grant_type=refresh_token",
            headers=self._headers(),
            json={"refresh_token": state["refresh_token"]},
        )
        data = _json(resp)
        if resp.status_code != 200 or not isinstance(data, dict) or "access_token" not in data:
            raise ProviderDown(f"{self.name}: token refresh refused (HTTP {resp.status_code})")
        fresh = {
            **state,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or state["refresh_token"],
        }
        self._tokens[address.value] = fresh
        return fresh

    async def _read(self, address: Address, params: dict[str, str]) -> Any:
        """GET the emails table, refreshing an expired token once."""
        state = self._state(address)
        for attempt in range(2):
            resp = await self.http.send(
                "GET",
                f"{PROJECT}/rest/v1/emails",
                params=params,
                headers=self._headers(state["access_token"]),
            )
            if resp.status_code != 401:
                return resp
            if attempt == 0:
                state = await self._refresh(address, state)
        raise ProviderDown(f"{self.name}: a refreshed token was rejected")

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.domain and opts.domain not in DOMAINS:
            raise NotSupported(f"{self.name} does not serve {opts.domain}")
        value = f"{opts.local or f'cb{secrets.token_hex(5)}'}@{opts.domain or DOMAINS[0]}"
        auth = await self.http.json(
            "POST", f"{PROJECT}/auth/v1/signup", headers=self._headers(), json={"data": {}}
        )
        user = auth.get("user") if isinstance(auth, dict) else None
        if not (isinstance(user, dict) and "access_token" in auth and "refresh_token" in auth):
            raise SchemaDrift(self.name, expected="tokens and a user from signup", got=auth)
        expires = datetime.now(UTC) + LIFETIME
        rows = await self.http.json(
            "POST",
            f"{PROJECT}/rest/v1/mailboxes",
            headers={**self._headers(auth["access_token"]), "Prefer": "return=representation"},
            json={
                "user_id": user["id"],
                "address": value,
                "mailbox_type": "random",
                "expires_at": expires.isoformat(),
            },
        )
        row = rows[0] if isinstance(rows, list) and rows else rows
        if not isinstance(row, dict) or "id" not in row:
            raise SchemaDrift(self.name, expected="the created mailbox row", got=rows)
        state = {
            "access_token": auth["access_token"],
            "refresh_token": auth["refresh_token"],
            "mailbox_id": row["id"],
        }
        return Address(
            value=value,
            provider=self.name,
            expires_at=expires,
            state=json.dumps(state, separators=(",", ":")),
        )

    async def list(self, address: Address) -> list[MessageRow]:
        state = self._state(address)
        resp = await self._read(
            address,
            {
                "select": "*",
                "mailbox_id": f"eq.{state['mailbox_id']}",
                "folder": "eq.inbox",
                "order": "received_at.desc",
                "limit": "50",
            },
        )
        rows = _json(resp)
        if resp.status_code != 200 or not isinstance(rows, list):
            raise SchemaDrift(self.name, expected="an array of email rows", got=rows)
        if not all(isinstance(r, dict) and "id" in r for r in rows):
            raise SchemaDrift(self.name, expected="email rows with 'id'", got=rows)
        return [
            MessageRow(
                id=str(r["id"]),
                sender=_sender(r),
                subject=r.get("subject") or "",
                received_at=_at(r.get("received_at")),
            )
            for r in rows
        ]

    async def get(self, address: Address, id: str) -> Message:
        resp = await self._read(address, {"select": "*", "id": f"eq.{id}"})
        rows = _json(resp)
        # A malformed id is a 400 from Postgres; either way the message is not there.
        if resp.status_code == 400 or rows == []:
            raise MessageGone(f"{self.name}: message {id} is gone")
        if resp.status_code != 200 or not isinstance(rows, list) or not isinstance(rows[0], dict):
            raise SchemaDrift(self.name, expected="one email row", got=rows)
        row = rows[0]
        html = row.get("html_body") or ""
        return Message(
            id=id,
            sender=_sender(row),
            subject=row.get("subject") or "",
            received_at=_at(row.get("received_at")),
            html=html,
            text=row.get("body") or html_to_text(html),
            links=extract_links(html or row.get("body") or ""),
        )


def _json(resp: Any) -> Any:
    try:
        return json.loads(resp.text)
    except ValueError:
        return None
