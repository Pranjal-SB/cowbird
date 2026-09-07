"""The only tests that prove mail actually arrives and can be read.

Every other test in this repo stops at `generate()` and `list()`. The contract's
body-read test skips when the inbox is empty, which live it always is, and the
canary deliberately never calls `get()`. So the read path — `get()`, the
HTML-to-text pass, link extraction, `otp()` — has been proven only against
recorded fixtures.

Two senders, because they prove different things:

- **sendtestemail.com** needs no credentials and no setup, so this gate runs for
  anyone who clones the repo. It proves the whole delivery path end to end:
  generate, deliver, poll, list, body-read, parse, extract links. What it cannot
  prove is OTP extraction, because the message content belongs to them.
- **SMTP**, when credentials are configured, sends a body we control, which is
  the only way to prove `otp()` returns the code that was actually sent.

Set these in `.env` at the repo root (gitignored) to enable the second one. Any
credentialed sender works — a Gmail app password, a free Brevo or SendGrid key,
your own postfix:

    COWBIRD_SMTP_HOST=smtp.gmail.com
    COWBIRD_SMTP_PORT=587
    COWBIRD_SMTP_USER=you@example.com
    COWBIRD_SMTP_PASSWORD=your-app-password
    COWBIRD_SMTP_FROM=you@example.com

Both are `live`-marked, so the default offline suite is unaffected.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from pathlib import Path

import pytest
from cowbird.health import HealthStore
from cowbird.inbox import Inbox
from cowbird.pool import Pool, Request
from cowbird.registry import Registry
from cowbird.transport import Transport

pytestmark = pytest.mark.live

# How long to wait for delivery. Disposable backends poll their own spools and a
# cold one can sit on a first message for the better part of a minute. Too short
# a timeout turns a working provider into a flaky test, which is worse than no
# test at all.
DELIVERY_TIMEOUT = 180.0

SENDTESTEMAIL_FORM = "https://sendtestemail.com/"
SENDTESTEMAIL_POST = "https://sendtestemail.com/?act=send-test-email"
# The form carries a rotating hidden token. Unquoted attribute, hence the loose
# character class.
_TOKEN = re.compile(r"name=us value=([^\s>]+)")

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def installed_providers() -> list[str]:
    return sorted(p.name for p in Registry().all())


async def fresh_inbox(provider_name: str) -> tuple[Inbox, Pool]:
    health = HealthStore()
    pool = Pool(Registry(health=health), health)
    provider, address = await pool.acquire(Request(provider=provider_name))
    return Inbox(provider, address), pool


# --------------------------------------------------------------------------
# Sender 1: no credentials required
# --------------------------------------------------------------------------


async def send_via_sendtestemail(to: str) -> None:
    """Ask sendtestemail.com to deliver a message to `to`.

    Goes through `Transport` rather than a bare client so the request gets the
    same impersonation, retry and challenge detection every provider call does —
    and so an anti-bot wall here surfaces as `CloudflareChallenge` rather than a
    confusing assertion failure.

    The form carries a hidden `us` token which is required: posting without it
    returns the page as normal and silently sends nothing (verified 2026-09-07).
    The token is also **not always offered** — after a couple of sends from one
    IP the field disappears, which reads as a per-IP quota. That is a property of
    their service, not a defect here, so it skips rather than fails. A gate that
    goes red because someone else's quota ran out gets muted within a week, and
    then it is worth nothing on the day it should have caught something.
    """
    http = Transport("sendtestemail")
    try:
        page = await http.text("GET", SENDTESTEMAIL_FORM)
        match = _TOKEN.search(page)
        if not match:
            pytest.skip(
                "sendtestemail.com is not offering its form token right now "
                "(per-IP quota, most likely). Configure COWBIRD_SMTP_* for a "
                "sender that does not depend on someone else's rate limit."
            )
        await http.text(
            "POST",
            SENDTESTEMAIL_POST,
            data={"email_address": to, "us": match.group(1).strip('"')},
        )
    finally:
        await http.aclose()


@pytest.mark.parametrize("provider_name", installed_providers())
async def test_a_real_message_arrives_and_can_be_read(provider_name: str) -> None:
    box, pool = await fresh_inbox(provider_name)
    try:
        await send_via_sendtestemail(box.address.value)

        # The address was generated seconds ago, so anything in it came from
        # this run. No correlation token is needed to know the message is ours.
        #
        # Locked rows are skipped rather than read: emailnator seeds every fresh
        # address with its own paywalled promo message, so rows[0] is routinely
        # not the message we are waiting for. Reading it raises MessageLocked
        # and the test fails against a backend that is working fine.
        deadline = asyncio.get_running_loop().time() + DELIVERY_TIMEOUT
        rows: list = []
        while not rows and asyncio.get_running_loop().time() < deadline:
            rows = [row for row in await box.messages() if not row.locked]
            if not rows:
                await asyncio.sleep(5)
        assert rows, (
            f"{provider_name}: no readable message arrived within "
            f"{DELIVERY_TIMEOUT:.0f}s. Either the provider is not receiving, or "
            "sendtestemail.com is down or refusing this domain."
        )

        message = await box.get(rows[0].id)
        assert message.text.strip(), f"{provider_name}: message body read back empty"
        assert "email address is working" in message.text.lower(), (
            f"{provider_name}: body did not survive the read path intact; got "
            f"{message.text[:200]!r}"
        )
        # sendtestemail's body carries exactly one link. Asserting it proves
        # extract_links() ran over a real message rather than a fixture.
        assert any("sendtestemail.com" in href for href in message.links), (
            f"{provider_name}: no link extracted; got {message.links!r}"
        )
    finally:
        await box.aclose()
        await pool.registry.aclose()


# --------------------------------------------------------------------------
# Sender 2: needs credentials, proves otp()
# --------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Populate os.environ from the repo-root .env, without overriding anything
    already set. Five lines of parsing does not justify a dependency the runtime
    never needs."""
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _smtp_config() -> dict[str, str]:
    _load_dotenv()
    missing = [
        name
        for name in ("COWBIRD_SMTP_HOST", "COWBIRD_SMTP_USER", "COWBIRD_SMTP_PASSWORD")
        if not os.environ.get(name)
    ]
    if missing:
        pytest.skip(f"no controlled sender configured; set {', '.join(missing)}")
    user = os.environ["COWBIRD_SMTP_USER"]
    return {
        "host": os.environ["COWBIRD_SMTP_HOST"],
        "port": os.environ.get("COWBIRD_SMTP_PORT", "587"),
        "user": user,
        "password": os.environ["COWBIRD_SMTP_PASSWORD"],
        "sender": os.environ.get("COWBIRD_SMTP_FROM", user),
    }


def _send_smtp(config: dict[str, str], to: str, code: str) -> None:
    """Blocking send. Called through asyncio.to_thread so the event loop the
    inbox polls on keeps running during the SMTP conversation."""
    message = EmailMessage()
    message["Subject"] = "Your verification code"
    message["From"] = config["sender"]
    message["To"] = to
    # Plain text with the code standing alone, so the default strict six-digit
    # rule in extract_otp() is what gets exercised. A caller-supplied pattern
    # would prove the plumbing while hiding whether the default works.
    message.set_content(
        f"Your cowbird verification code is {code}.\n\nIt expires in 10 minutes.\n"
    )

    port = int(config["port"])
    if port == 465:
        with smtplib.SMTP_SSL(config["host"], port, timeout=30) as smtp:
            smtp.login(config["user"], config["password"])
            smtp.send_message(message)
        return
    with smtplib.SMTP(config["host"], port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(config["user"], config["password"])
        smtp.send_message(message)


@pytest.mark.parametrize("provider_name", installed_providers())
async def test_otp_reads_back_the_code_that_was_sent(provider_name: str) -> None:
    config = _smtp_config()
    code = f"{secrets.randbelow(1_000_000):06d}"

    box, pool = await fresh_inbox(provider_name)
    try:
        await asyncio.to_thread(_send_smtp, config, box.address.value, code)
        found = await box.otp(timeout=DELIVERY_TIMEOUT)
        assert found == code, (
            f"{provider_name} delivered a message but otp() returned the wrong "
            f"value: expected {code}, got {found}"
        )
    finally:
        await box.aclose()
        await pool.registry.aclose()
