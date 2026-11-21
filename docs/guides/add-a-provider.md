# Add a provider

A provider is an adapter for one email backend. It knows that backend's API
and translates it into cowbird's types. Everything else, such as HTTP,
retries, proxies, health tracking and HTML parsing, belongs to core.

## Pick one

The [provider inventory](../reference/provider-inventory.md) lists every known
backend that does not ship yet, with what is known about it and why some were
parked or dropped.

## Start from the worked example

`packages/providers/mailtm` is the reference adapter. Copy its layout:

```
packages/providers/<name>/
  pyproject.toml                 package cowbird-<name>, entry point below
  src/cowbird_<name>/__init__.py the adapter
  tests/test_contract.py         the contract suite, against recorded fixtures
  tests/test_live.py             the same calls against the real service
  tests/fixtures/                responses recorded from the real service
```

The entry point is how cowbird finds the provider:

```toml
[project.entry-points."cowbird.providers"]
<name> = "cowbird_<name>:YourProvider"
```

## Write the adapter

Subclass `cowbird.provider.Provider`. Set `name` and `caps` (a
`cowbird.Capabilities`: kind, sites, domains, lifetimes and the rest), then
implement:

- `generate(options)`: return an `Address`. If later calls need a password or
  session, put it in `Address.state` and set `needs_state=True`.
- `list(address)`: return `MessageRow`s.
- `get(address, id)`: return a `Message`.
- `delete(address, id)` and `watch(address)`, only if the backend supports
  them. The default `watch()` polls `list()`.

Aim for about 40 lines of real protocol knowledge. If an adapter handles
sessions, backoff or its own HTTP client, that work belongs in core instead.

Raise cowbird's errors, not your own: `SchemaDrift` when a response does not
have the shape you expect (this quarantines the provider), `ProviderDown` when
the service is unreachable, `MessageGone` for a message that disappeared.

## Pass the contract

`cowbird.contract.ProviderContract` is the suite every adapter must pass. In
`tests/test_contract.py`, subclass it and supply a `provider` fixture built on
`cowbird.testing.FakeTransport`, which replays your fixtures. The same suite
runs unchanged in `test_live.py` against the real service; only the transport
behind the fixture changes.

**Record fixtures through cowbird's `Transport`, never with `curl`.** The two
send different headers, and some backends answer them with different shapes.
A fixture captured with `curl` once gave a fully green suite against a payload
the real code never receives.

## Check it

```bash
uv run pytest packages/providers/<name>
uv run pytest packages/providers/<name> -m live
uv run python scripts/gen_docs.py
uv run ruff check .
```

The generator adds the new provider to [Providers](../reference/providers.md).
Move its line in the provider inventory to the shipped section.
