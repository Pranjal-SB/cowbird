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
see `cowbird.pool.Request` for the full set.

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

## HTTP API

`cowbird-server` is a FastAPI app in `packages/server`. Run it locally with:

```bash
API_KEYS=local-dev uv run uvicorn cowbird_server:create_app --factory --host 0.0.0.0 --port 8000
```

or build the image at the repo root: `docker build -t cowbird-server .` then
`docker run --rm -e API_KEYS=local-dev -p 8000:8000 cowbird-server`. The
container listens on `$PORT` and defaults to 8000, so hosts that assign a port
(Render, Koyeb, Fly) need no extra configuration. It runs as a non-root user and
carries a `HEALTHCHECK` against `/health`.

```
POST   /v1/inboxes                     {"kind": "gmail-alias"} and other capability filters
GET    /v1/inboxes/{addr}/messages
GET    /v1/inboxes/{addr}/messages/{id}
GET    /v1/inboxes/{addr}/wait?otp=1   long-poll, server-capped
DELETE /v1/inboxes/{addr}/messages/{id}
POST   /v1/webhooks                    one-shot, SSRF-guarded
GET    /v1/webhooks
DELETE /v1/webhooks/{id}
GET    /v1/providers                   health matrix
GET    /health                         liveness, unauthenticated
```

```
$ curl -s -X POST -H "x-api-key: local-dev" localhost:8000/v1/inboxes
{"success":true,"data":{"address":"cbdf0cddf3a2@getnada.com","provider":"inboxes","expires_at":null},"error":null}

$ curl -s -H "x-api-key: local-dev" localhost:8000/v1/providers
{"success":true,"data":[{"provider":"emailnator","status":"ok","p50":1.11,"kind":["gmail-alias"],"sites":["emailnator.com"]},{"provider":"inboxes","status":"ok","p50":0.0,"kind":["own-domain"],"sites":["inboxes.com"]},{"provider":"mailtm","status":"ok","p50":1.36,"kind":["own-domain"],"sites":["mail.tm"]}],"error":null}
```

Every route except `/health` requires an `x-api-key` header; valid keys come
from `API_KEYS` (comma-separated). `/wait` is capped server-side at `WAIT_MAX`
seconds (25 by default) regardless of the `timeout` a client asks for; a
client that needs to keep waiting just re-issues the request. Issued addresses
live in Postgres when `DATABASE_URL` is set, which is what lets several
instances serve each other's addresses; without it they live in process
memory and a restart drops them. Webhook registrations always live in process
memory: they are one-shot and capped at `WEBHOOK_MAX`, so a restart drops
pending ones. Fine for one-shot automation, not something to rely on across a
redeploy.

### Running more than one instance

```bash
$ docker compose up -d --build
$ curl -H "x-api-key: local-dev" -X POST http://127.0.0.1:8000/v1/inboxes
{"success":true,"data":{"address":"cb.example.one@gmail.com","provider":"emailnator","expires_at":null},"error":null}
```

Caddy round-robins port 8000 across two servers sharing one Postgres. The
instances are also published individually on 8001 and 8002, which is how the
cross-instance tests address them.

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

The deliverable is an HTTP API, and it ships: `cowbird-server` in
`packages/server`, with API-key auth, capped long-poll, one-shot webhooks and
per-key rate limiting. The CLI is a convenience for working on the library and
is not the product. Shared health state across instances and Postgres-backed
storage are not built yet.
