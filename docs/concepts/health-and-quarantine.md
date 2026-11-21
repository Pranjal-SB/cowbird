# Health and quarantine

Free email services break often: they go down, put Cloudflare in front, or
change their API. cowbird finds out from the calls it makes, not from a table
someone edits by hand.

## The four states

| Status | Meaning | Routed? |
|---|---|---|
| `ok` | Working. | Yes |
| `slow` | Working, but over three times slower than its own recent median. | Yes, after `ok` providers |
| `down` | The last call failed: unreachable, an error, or a Cloudflare challenge. | Retried once a minute |
| `quarantined` | The service answered with a shape the adapter does not understand. | No, until cleared |

The difference between the last two matters. `down` is usually temporary and
not the adapter's fault, so cowbird tries the provider again after 60 seconds,
one caller at a time. `quarantined` means the service changed and the adapter
is now wrong. Retrying will not help; someone has to fix the adapter.

## Seeing it

```bash
uv run cowbird providers
```

prints each provider's status and median latency. Over HTTP,
`GET /v1/providers` returns the same.

The CLI keeps health between runs in `~/.cowbird/health.json`
(`COWBIRD_HEALTH_PATH` moves it). Servers sharing a Postgres share
quarantines; each instance keeps its own latency, because each measures from
its own network.

## Clearing a quarantine

After fixing the adapter:

```bash
curl -X DELETE -H "x-api-key: $KEY" localhost:8000/v1/providers/<name>/quarantine
```

This clears it on every instance that shares the database.

## The weekly canary

```bash
uv run cowbird canary
```

tries every provider for real: get an address, list its inbox. It prints one
status per provider. CI runs it every Monday, and when a provider is
quarantined it opens a GitHub issue with the failure detail from
`cowbird canary --json`.

A provider that is only `down` does not fail the run. CI runs on data-centre
IP addresses, and Cloudflare challenges those where a real user's connection
would pass. Only `quarantined` fails it.
