"""etempmail.com: .edu.pl addresses behind a Cloudflare Turnstile.

Creating an address costs one Turnstile solve, sent as the form field
`cf_token` to /getEmailAddress. A call without a valid token either gets a 403
"Bot detected!" or, with no token at all, a decoy `never.gonna.give.you.up...`
address; both mean the token was refused, so both are the solver's fault.

The inbox is keyed on the CodeIgniter `ci_session` cookie: on a shared jar a
second create hands back the first address. So every request runs on a fresh
session and the cookie travels in Address.state; whoever holds it reads the mail.
A session the site no longer knows gets an empty body from /getInbox.

/getInbox answers with every message in full (`subject`, `from`, `date`, `body`)
and no id; the site's own /email?id=N is a 1-based index into it that renders
the body into an iframe behind Cloudflare's address obfuscation. get() reads
the listing instead, and ids are a hash of the row, so they do not shift when
new mail arrives. Reading mail never costs a solve.

The domain is the site's pick: its /changeEmailAddress answers 404.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

from cowbird.errors import AddressExpired, MessageGone, NotSupported, SchemaDrift, SolverUnavailable
from cowbird.models import Address, Capabilities, Kind, Message, MessageRow
from cowbird.parsing import extract_links, html_to_text
from cowbird.provider import GenerateOptions, Provider

SITE = "https://etempmail.com"
PAGE = f"{SITE}/"
SITEKEY = "0x4AAAAAADgCwAbwHVdgg5az"
COOKIE = "ci_session"
DOMAINS = ("temporarmail.edu.pl", "mats.edu.pl", "securemail.edu.pl")
# The page counts an address down from `creation_time` over `data-minutes`.
LIFE = timedelta(minutes=20)
# Row dates are Istanbul wall-clock time, which has no DST.
SITE_TZ = timezone(timedelta(hours=3))
DECOY = "never.gonna.give.you.up"
ROW_FIELDS = ("subject", "from", "date", "body")


def _at(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d/%m/%Y %H:%M:%S").replace(tzinfo=SITE_TZ).astimezone(UTC)
    except ValueError:
        return None


def _id(row: dict) -> str:
    raw = json.dumps([row[k] for k in ROW_FIELDS], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class ETempMail(Provider):
    name = "etempmail"
    caps = Capabilities(
        kind=frozenset({Kind.EDU}),
        sites=("etempmail.com",),
        domains=DOMAINS,
        domain_count=len(DOMAINS),
        address_ttl=LIFE,
        # Mail lives as long as its inbox.
        message_ttl=None,
        push=False,
        delete=False,
        custom_local=False,
        self_hosted=False,
        needs_residential_ip=False,
        needs_state=True,
        needs_solver=True,
        fresh_session=True,
        # Every create waits on a solve (~7s).
        max_concurrency=2,
        poll_interval=5.0,
    )

    async def generate(self, opts: GenerateOptions | None = None) -> Address:
        opts = opts or GenerateOptions()
        if opts.local or opts.domain:
            raise NotSupported("etempmail picks both the local part and the domain")
        if self.http.solver is None:
            raise NotSupported(f"{self.name} needs COWBIRD_SOLVER_URL")
        token = await self.http.solver.turnstile(PAGE, SITEKEY)
        resp = await self.http.send("POST", f"{SITE}/getEmailAddress", data={"cf_token": token})
        try:
            data = json.loads(resp.text)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="JSON from create", got=resp.text[:200]) from exc
        if resp.status_code == 403 and isinstance(data, dict) and data.get("success") is False:
            raise SolverUnavailable(f"etempmail refused the Turnstile token: {data.get('message')}")
        address = data.get("address") if isinstance(data, dict) else None
        if isinstance(address, str) and address.startswith(DECOY):
            raise SolverUnavailable("etempmail answered with its decoy address: token refused")
        created = data.get("creation_time") if isinstance(data, dict) else None
        session = resp.cookies.get(COOKIE)
        if not (
            resp.status_code == 200
            and isinstance(address, str)
            and "@" in address
            and isinstance(created, str)
            and created.isdigit()
            and session
        ):
            raise SchemaDrift(
                self.name,
                expected=f"'address', 'creation_time' and a {COOKIE} cookie from create",
                got=data,
            )
        return Address(
            value=address,
            provider=self.name,
            expires_at=datetime.fromtimestamp(int(created), UTC) + LIFE,
            state=json.dumps({COOKIE: session}, separators=(",", ":")),
        )

    async def _rows(self, address: Address) -> list[dict]:
        try:
            cookies = {COOKIE: json.loads(address.state or "")[COOKIE]}
        except (ValueError, TypeError, KeyError) as exc:
            raise NotSupported("etempmail needs the session cookie issued by generate()") from exc
        body = await self.http.text("POST", f"{SITE}/getInbox", cookies=cookies)
        if not body.strip():
            raise AddressExpired(f"etempmail: {address.value} is gone")
        try:
            rows = json.loads(body)
        except ValueError as exc:
            raise SchemaDrift(self.name, expected="JSON from inbox", got=body[:200]) from exc
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and all(isinstance(r.get(k), str) for k in ROW_FIELDS) for r in rows
        ):
            raise SchemaDrift(self.name, expected=f"inbox rows with {ROW_FIELDS}", got=rows)
        return rows

    @staticmethod
    def _row(r: dict) -> MessageRow:
        return MessageRow(
            id=_id(r), sender=r["from"], subject=r["subject"], received_at=_at(r["date"])
        )

    async def list(self, address: Address) -> list[MessageRow]:
        return [self._row(r) for r in await self._rows(address)]

    async def get(self, address: Address, id: str) -> Message:
        found = next((r for r in await self._rows(address) if _id(r) == id), None)
        if found is None:
            raise MessageGone(f"etempmail: message {id} is gone")
        row, html = self._row(found), found["body"]
        return Message(
            id=id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            html=html,
            text=html_to_text(html),
            links=extract_links(html),
        )
