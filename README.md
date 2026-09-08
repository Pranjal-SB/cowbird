# cowbird

Cowbird fronts disposable-email backends behind one interface: generate an
address, wait for a message, extract an OTP or a link, done. It is named for
the brood parasite. It owns no mail infrastructure of its own and uses
everyone else's.

## Install

```bash
uv sync --all-packages --all-extras
```

Requires Python 3.12+.

## Library

```python
async with cowbird.inbox() as box:
    print(box.address)
    code = await box.otp(timeout=120)
```

`inbox()` picks the best healthy provider for you. Pass `provider=`, `kind=`,
or a capability filter (`address_ttl=`, `domain_not_in=`, ...) to narrow it;
see `docs/superpowers/specs/2026-09-05-cowbird-design.md` for the full set.

## CLI

```
$ uv run cowbird new
cb94091c592b@tupmail.com	inboxes	ttl forever

$ uv run cowbird new --json
{"address": "cbcb4a38cb50@gimpmail.com", "provider": "inboxes", "address_ttl": null, "state": null}

$ uv run cowbird new --gmail
cb.example.two@gmail.com	emailnator	ttl forever

$ uv run cowbird providers
PROVIDER        STATUS        P50      KIND                      SITES
emailnator      ok            2.3s     gmail-alias               emailnator.com
inboxes         ok            0.0s     own-domain                inboxes.com
mailtm          ok            1.4s     own-domain                mail.tm
```

`--json` includes `state`: some providers (mail.tm included) bind an inbox to
a credential issued at generate time, and that has to travel to a separate
`cowbird wait --state ...` process to read the same inbox back. Treat it as a
secret.

## Provider health

`cowbird providers` prints a live matrix, not a declared one: STATUS and P50
come from `HealthStore`, which tracks every call each provider actually makes
and marks a backend down after it fails, not after someone edits a table by
hand.

`cowbird canary` probes every installed provider live, generate then list, and
prints one line each. CI runs it on a schedule every Monday, and a provider
that quarantines files a GitHub issue with `--json` output in the body so the
drift detail is in the issue itself. A backend simply being unreachable does
not fail the build: a datacenter runner IP can draw a Cloudflare challenge that
a real user never sees, so only a schema drift turns it red.

```
$ uv run cowbird canary
emailnator	ok
inboxes	ok
mailtm	ok
```

## Push

`watch()` is part of the provider protocol and providers declare `push` in
their capabilities, but every provider shipped so far polls. Nothing exercises
the push path yet. The intended first push backend was dropmail over
WebSocket, and its free API token path has since closed.

## Testing

```bash
uv run pytest -m "not live"    # offline, the default gate
uv run pytest -m live          # hits real backends
```

Adapters inherit a shared contract suite (`cowbird.contract.ProviderContract`)
that runs unchanged in both modes. Only the transport behind the fixture
swaps. Fixtures must be recorded through `Transport`, never `curl`: the two
send different `Accept` headers and mail.tm answers them with different
shapes, which once produced a fully green suite against a payload the runtime
never sees.

`packages/core/tests/test_delivery.py` proves mail actually arrives. Everything
else stops at `list()`.

It asks `sendtestemail.com` to deliver a message to every installed provider,
then reads it back: generate, deliver, poll, body-read, parse, extract links.
No account and no key, so `uv run pytest -m live` runs it as-is. The catch is
that sendtestemail rations its form token per IP, and the test skips rather
than fails when it does not get one, which is often. A skip there means the
sender was unavailable, not that a provider is broken.

A second test proves `otp()` returns the code that was actually sent, which
needs a body under our control and therefore a sender we own. Any credentialed
sender works (a Gmail app password, a free Brevo or SendGrid key, your own
postfix). Put it in `.env` at the repo root, which is gitignored:

```
COWBIRD_SMTP_HOST=smtp.gmail.com
COWBIRD_SMTP_PORT=587
COWBIRD_SMTP_USER=you@example.com
COWBIRD_SMTP_PASSWORD=your-app-password
COWBIRD_SMTP_FROM=you@example.com
```

Unset, that second test skips; the first still runs. Both are `live`-marked, so
the offline suite is unaffected either way.

## Adding a provider

Read `packages/core/src/cowbird/contract.py` for the contract
(`ProviderContract`) every adapter must pass, and
`packages/providers/mailtm/src/cowbird_mailtm/__init__.py` as the worked
example. Core owns transport, retries, proxying, health tracking, and HTML
parsing; an adapter's job is to know one backend's API shape and translate it
to `Address` / `MessageRow` / `Message`. Target size is around 40 lines of
actual protocol knowledge. If an adapter is doing session handling, backoff,
or its own HTTP client, that belongs in core instead.

`docs/PROVIDERS.md` has the backlog of ~60 known backends: which ship, which
are parked and why, and which are seeded, starred, or self-hostable.

## Status

Three providers ship and are verified against the live services: mail.tm,
emailnator (Gmail aliases) and inboxes.com. Two more from the seed set are
parked after recon, with the reasons written up in `docs/recon/`: smailpro
needs a solved Cloudflare Turnstile token on every call, and tempr.email's
message-read endpoint was never observed.

The deliverable is an HTTP API. The CLI is a convenience for working on the
library and is not the product. That server is Plan 3, along with shared health
state so several API instances agree on which backends are rotten. Full design:
`docs/superpowers/specs/2026-09-05-cowbird-design.md`.
