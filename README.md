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
cb94ae406a61@uberip.com	mailtm	ttl forever

$ uv run cowbird new --json
{"address": "cbad93314265@uberip.com", "provider": "mailtm", "address_ttl": null, "state": "..."}

$ uv run cowbird providers
PROVIDER        STATUS        P50      KIND                      SITES
mailtm          ok            -        own-domain                mail.tm
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

## Adding a provider

Read `packages/core/src/cowbird/contract.py` for the contract
(`ProviderContract`) every adapter must pass, and
`packages/providers/mailtm/src/cowbird_mailtm/__init__.py` as the worked
example. Core owns transport, retries, proxying, health tracking, and HTML
parsing; an adapter's job is to know one backend's API shape and translate it
to `Address` / `MessageRow` / `Message`. Target size is around 40 lines of
actual protocol knowledge. If an adapter is doing session handling, backoff,
or its own HTTP client, that belongs in core instead.

`docs/PROVIDERS.md` has the backlog of ~60 known backends and which ones are
seeded, starred, or self-hostable.

## Status

mail.tm ships today, verified against the live service.
The rest of the seed set (emailnator, smailpro, tempr.email, inboxes,
dropmail) and the HTTP server come later.
