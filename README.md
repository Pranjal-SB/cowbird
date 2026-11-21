# cowbird

Disposable inboxes from dozens of free email services, behind one interface.
Get an address, wait for the message, take the code or the link. Use it as a
Python library, from the command line, or over HTTP.

Named for the brood parasite: cowbird runs no mail servers of its own and uses
everyone else's.

## Highlights

- **Made for agents.** JSON output, one call that waits for mail and returns
  the one-time code, and one response shape for every API call, errors
  included. An agent needs five calls, listed in
  [Using cowbird from an agent](docs/guides/use-from-an-agent.md).
- **44 providers, one interface.** Own-domain addresses plus Gmail and Outlook
  aliases, which many signup forms accept where a throwaway domain is refused.
  [The full list](docs/reference/providers.md).
- **Picks a working provider for you.** cowbird measures every call it makes,
  skips providers that are down, and retries them after a minute. A provider
  whose responses change shape is taken out of rotation until someone looks.
- **Checked every week.** A scheduled canary probes every provider and opens
  an issue when one breaks.
- **An HTTP API to run yourself.** API keys, long-poll waits, one-shot
  webhooks, and shared Postgres state so several instances can sit behind one
  load balancer.

## Install

```bash
git clone https://github.com/Pranjal-SB/cowbird.git
cd cowbird
uv sync --all-packages --all-extras
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).
[Installation](docs/getting-started/installation.md) has the details.

## Quick start

The same job three ways: get an address, then wait for a code.

**Python**

```python
import cowbird

async with cowbird.inbox() as box:
    print(box.address.value)        # sign up with this
    code = await box.otp(timeout=120)
```

**Command line**

```bash
$ uv run cowbird new --json
{"address": "cb1f0a2e@example.test", "provider": "mailtm", "address_ttl": null, "state": "..."}
$ uv run cowbird wait cb1f0a2e@example.test --provider mailtm --state '...' --otp
482913
```

**HTTP**

```bash
$ curl -s -X POST -H "x-api-key: $KEY" localhost:8000/v1/inboxes
{"success":true,"data":{"address":"cb1f0a2e@example.test","provider":"mailtm","expires_at":null},"error":null}
$ curl -s -H "x-api-key: $KEY" "localhost:8000/v1/inboxes/cb1f0a2e@example.test/wait?otp=true"
{"success":true,"data":{"id":"m1","subject":"Your code","otp":"482913",...},"error":null}
```

## Coming next

- an MCP server, so agents can use cowbird as a tool directly;
- an agent skill;
- packages on PyPI.

## Documentation

The [documentation](docs/README.md) has four parts:

- [Getting started](docs/README.md#getting-started): installation and your
  first inbox.
- [Guides](docs/README.md#guides): use cowbird from an agent, run the server,
  the Cloudflare solver, add a provider, test live delivery.
- [Concepts](docs/README.md#concepts): providers and routing, health and
  quarantine, push and polling.
- [Reference](docs/README.md#reference): [library](docs/reference/library.md),
  [CLI](docs/reference/cli.md), [HTTP API](docs/reference/http-api.md),
  [settings](docs/reference/settings.md),
  [providers](docs/reference/providers.md) and
  [troubleshooting](docs/reference/troubleshooting.md).

## Contributing

New providers are the most useful contribution:
[Add a provider](docs/guides/add-a-provider.md) walks through one, and the
[provider inventory](docs/reference/provider-inventory.md) lists the
candidates. Before sending a change:

```bash
uv run pytest -m "not live"
uv run ruff check .
```

If you change the CLI, the HTTP API, the server settings or the set of
providers, run `uv run python scripts/gen_docs.py` to update the reference
pages.

## Licence

[AGPL-3.0](LICENSE).
