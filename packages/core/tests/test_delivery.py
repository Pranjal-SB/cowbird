"""The only tests that prove mail actually arrives and can be read.

Every other test in this repo stops at `generate()` and `list()`. The contract's
body-read test skips when the inbox is empty, which live it always is, and the
canary deliberately never calls `get()`. So the read path — `get()`, the
HTML-to-text pass, link extraction, `otp()` — has been proven only against
recorded fixtures.

Two senders, because they prove different things:

- **testemailsender.com** (JoltMx) needs no credentials and no setup, so this
  gate runs for anyone who clones the repo. It proves the whole delivery path
  end to end: generate, deliver, poll, list, body-read, parse, extract links.
  What it cannot prove is OTP extraction, because the message content belongs
  to them. It allows about a dozen sends per network per day, one per
  provider, so run it once, not in a loop.
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
import secrets
import smtplib
from email.message import EmailMessage
from pathlib import Path

import pytest
from cowbird.errors import CloudflareChallenge, ProviderDown, RateLimited
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

JOLTMX_SENDS = "https://testemailsender.com/api/tools/test-email/sends"
JOLTMX_HEADERS = {"referer": "https://testemailsender.com/"}
JOLTMX_SENDER = "sendtest.joltmx.com"

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


async def send_via_joltmx(to: str) -> None:
    """Ask testemailsender.com to deliver its fixed test message to `to`.

    Goes through `Transport` so an anti-bot wall or a 429 surfaces as a typed
    error. Every way this can fail is about the sender, not the provider, so
    they all skip: the daily per-network limit (429), JoltMx itself being down,
    and the receiving server refusing JoltMx outright (maildrop answers
    `554 Invalid FCRDNS`). A gate that goes red because someone else's quota
    ran out gets muted within a week, and then it is worth nothing on the day
    it should have caught something.
    """
    http = Transport("joltmx")
    try:
        try:
            sent = await http.json(
                "POST", JOLTMX_SENDS, json={"recipientEmail": to}, headers=JOLTMX_HEADERS
            )
            status_url = f"{JOLTMX_SENDS}/{sent['id']}?token={sent['token']}"
            deadline = asyncio.get_running_loop().time() + DELIVERY_TIMEOUT
            status = sent
            while status["status"] == "Queued" and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(5)
                status = await http.json("GET", status_url, headers=JOLTMX_HEADERS)
        except (RateLimited, ProviderDown, CloudflareChallenge) as exc:
            pytest.skip(f"JoltMx could not send: {exc!r}")
        if status["status"] == "Failed":
            pytest.skip(f"receiving server refused JoltMx: {status.get('responseText')}")
    finally:
        await http.aclose()


@pytest.mark.parametrize("provider_name", installed_providers())
async def test_a_real_message_arrives_and_can_be_read(provider_name: str) -> None:
    box, pool = await fresh_inbox(provider_name)
    try:
        # A fresh address is not an empty one. guerrillamail sends its own
        # welcome mail, and a Gmail alias is shared with whoever used it
        # before. Only a row that is new since this snapshot and comes from
        # JoltMx is ours.
        # The sender is checked on the fetched message, not the list row: a
        # row's sender can be only a display name on some backends.
        seen = {row.id for row in await box.messages()}
        await send_via_joltmx(box.address.value)

        deadline = asyncio.get_running_loop().time() + DELIVERY_TIMEOUT
        message = None
        while message is None and asyncio.get_running_loop().time() < deadline:
            for row in await box.messages():
                if row.id in seen or row.locked:
                    continue
                seen.add(row.id)
                candidate = await box.get(row.id)
                if JOLTMX_SENDER in candidate.sender:
                    message = candidate
                    break
            else:
                await asyncio.sleep(5)
        assert message is not None, (
            f"{provider_name}: JoltMx reported the message delivered, but it did "
            f"not show up within {DELIVERY_TIMEOUT:.0f}s."
        )

        assert "everything is working as expected" in message.text.lower(), (
            f"{provider_name}: body did not survive the read path intact; got "
            f"{message.text[:200]!r}"
        )
        # Every JoltMx message carries a per-recipient opt-out link. Asserting
        # it proves extract_links() ran over a real message, not a fixture.
        assert any("joltmx.com/tools/test-email/opt-out/" in href for href in message.links), (
            f"{provider_name}: no opt-out link extracted; got {message.links!r}"
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
