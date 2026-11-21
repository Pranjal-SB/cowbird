# Test live delivery

The offline suite proves each adapter handles its backend's responses. It does
not prove mail arrives. The live tests do.

```bash
uv run pytest -m "not live"    # offline, the default and what CI runs
uv run pytest -m live          # against the real services
```

## Delivery to every provider

`packages/core/tests/test_delivery.py` asks
[testemailsender.com](https://testemailsender.com) (JoltMx) to send a message
to every installed provider, then reads it back: generate, deliver, wait, read
the body, parse, pull out links. It needs no account or key.

JoltMx allows about a dozen sends per network per day, which is one run. Past
that limit, or when a provider's mail server refuses JoltMx (maildrop does),
the test skips instead of failing. A skip means the sender was unavailable,
not that the provider is broken.

## The code that was sent

A second test proves `otp()` returns the exact code that was sent. That needs
a message body you control, so it needs your own sender. Any SMTP account
works: a Gmail app password, a free Brevo or SendGrid key, or your own
postfix. Put it in `.env` at the repo root, which git ignores:

```
COWBIRD_SMTP_HOST=smtp.gmail.com
COWBIRD_SMTP_PORT=587
COWBIRD_SMTP_USER=you@example.test
COWBIRD_SMTP_PASSWORD=your-app-password
COWBIRD_SMTP_FROM=you@example.test
```

Without it, this test skips and the first one still runs.

## Some providers refuse some senders

A provider's mail server may refuse mail from free senders. tempr.email's does,
which is why its delivery is still unproven; see
[Push and polling](../concepts/push-and-polling.md).
