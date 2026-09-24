"""mailticking.com — Gmail aliases handed out by the web client's endpoints.

generate() asks /get-mailbox for an alias, activates it, then reads the
mailbox's 96-hex code off the home page, which renders it for whichever
address the `active_mailbox` cookie names. The listing takes the address and
that code and nothing else, so the code travels in Address.state and is as
good as a password for the inbox.

A body is read by message code with the `active_mailbox` cookie. Its answer
carries empty sender and subject fields, so get() takes those from the
listing row.

Only the Gmail types (1 dot, 2 plus, 3 googlemail) are requested; type 4 is
the site's own domain.
"""

from __future__ import annotations

import html as htmllib
import json as jsonlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from cowbird.errors import MessageGone, NotSupported, SchemaDrift
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://www.mailticking.com"
GMAIL_TYPES = ["1", "2", "3"]

_INPUT = re.compile(r"<input\b[^>]*\bid=['\"]active-mail['\"][^>]*>")
_CODE = re.compile(r"\bdata-code=['\"]([0-9a-fA-F]+)['\"]")
_VALUE = re.compile(r"\svalue=['\"]([^'\"]*)['\"]")


def _cookie(address: Address) -> dict[str, str]:
    return {"active_mailbox": quote(address.value)}


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(row["SendTime"]), UTC)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _sender(row: dict) -> str:
    address, name = row.get("FromEmail") or "", row.get("FromName") or ""
    return f"{name} <{address}>" if name and address else address


def _row(row: dict) -> MessageRow:
    return MessageRow(
        id=str(row["Code"]),
        sender=_sender(row),
        subject=row.get("Subject") or "",
        received_at=_at(row),
    )


class MailTicking(Provider):
    name = "mailticking"
    caps = Capabilities(
        kind=frozenset({Kind.GMAIL_ALIAS}),
        sites=("mailticking.com",),
        domains=("gmail.com", "googlemail.com"),
        domain_count=2,
        # Not measured.
        address_ttl=None,
        message_ttl=timedelta(days=1),
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        # Answers 429 "Too many requests, please slow down" when hit fast.
        max_concurrency=1,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        if opts and (opts.local or opts.domain):
            raise NotSupported("mailticking picks both the local part and the domain")
        box = await self.http.json("POST", f"{SITE}/get-mailbox", json={"types": GMAIL_TYPES})
        if not isinstance(box, dict) or not all(
            isinstance(box.get(k), str) and box[k] for k in ("email", "activate_token")
        ):
            raise SchemaDrift(
                self.name, expected="'email' and 'activate_token' from get-mailbox", got=box
            )
        email = box["email"]
        activated = await self.http.json(
            "POST",
            f"{SITE}/activate-email",
            json={"email": email, "source": "homepage", "activate_token": box["activate_token"]},
        )
        if not isinstance(activated, dict) or activated.get("success") is not True:
            raise SchemaDrift(self.name, expected="{'success': true} from activate", got=activated)
        address = Address(value=email, provider=self.name)
        page = await self.http.text("GET", f"{SITE}/", cookies=_cookie(address))
        tag = _INPUT.search(page)
        code = tag and _CODE.search(tag.group(0))
        shown = tag and _VALUE.search(tag.group(0))
        # The page renders whichever mailbox the cookie names; a different one
        # means the cookie was ignored and the code would open someone else's.
        if not code or not shown or htmllib.unescape(shown.group(1)) != email:
            raise SchemaDrift(
                self.name, expected=f"an #active-mail input for {email}", got=page[:200]
            )
        return Address(value=email, provider=self.name, state=code.group(1))

    async def list(self, address: Address) -> list[MessageRow]:
        if not address.state:
            raise NotSupported("mailticking needs the mailbox code issued by generate()")
        data = await self.http.json(
            "POST",
            f"{SITE}/get-emails",
            params={"lang": ""},
            json={"email": address.value, "code": address.state},
        )
        rows = data.get("emails") if isinstance(data, dict) else None
        if (
            not isinstance(rows, list)
            or data.get("success") is not True
            or not all(isinstance(r, dict) and r.get("Code") for r in rows)
        ):
            raise SchemaDrift(self.name, expected="'emails' rows with 'Code'", got=data)
        return [_row(r) for r in rows]

    async def get(self, address: Address, id: str) -> Message:
        row = next((r for r in await self.list(address) if r.id == id), None)
        if row is None:
            raise MessageGone(f"mailticking: message {id} is gone")
        resp = await self.http.send(
            "GET", f"{SITE}/mail/gmail-content/{quote(id, safe='')}", cookies=_cookie(address)
        )
        if resp.status_code == 404:
            raise MessageGone(f"mailticking: message {id} is gone")
        try:
            data = jsonlib.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="a JSON body", got=resp.text[:200]) from exc
        result = data.get("result") if isinstance(data, dict) else None
        if resp.status_code != 200 or not isinstance(result, dict):
            raise SchemaDrift(self.name, expected="a 'result' message object", got=data)
        html = result.get("content") or ""
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
