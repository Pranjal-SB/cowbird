# cowbird documentation

cowbird gives you disposable email addresses from many free services behind one
interface: a Python library, a command line, and an HTTP API. It picks a
provider that is working, waits for mail, and pulls out the one-time code or
link.

cowbird is not on PyPI yet. These pages describe running it from a clone of
the repository.

## Getting started

- [Installation](getting-started/installation.md): install cowbird and check
  which providers answer.
- [Your first inbox](getting-started/first-inbox.md): one address and one
  code, from Python, the command line and HTTP.

## Guides

One task per page.

- [Use cowbird from an agent](guides/use-from-an-agent.md): JSON output, exit
  codes, the response shape, waiting for a code.
- [Run the server](guides/run-the-server.md): uvicorn, Docker, and several
  instances on one Postgres.
- [Use a Cloudflare solver](guides/cloudflare-solver.md): unlock the providers
  behind Cloudflare Turnstile.
- [Add a provider](guides/add-a-provider.md): write an adapter and pass the
  contract suite.
- [Test live delivery](guides/test-live-delivery.md): prove mail really
  arrives.

## Concepts

How cowbird works, and why.

- [Providers and routing](concepts/providers-and-routing.md): kinds, filters,
  and how a provider gets picked.
- [Health and quarantine](concepts/health-and-quarantine.md): `ok`, `slow`,
  `down` and `quarantined`, and the weekly canary.
- [Push and polling](concepts/push-and-polling.md): how cowbird notices new
  mail.

## Reference

- [Library](reference/library.md): `cowbird.inbox()`, `Inbox` and the filters.
- [CLI](reference/cli.md): every command and flag (generated).
- [HTTP API](reference/http-api.md): every route and response (generated).
- [Settings](reference/settings.md): every environment variable (generated).
- [Providers](reference/providers.md): every provider that ships (generated).
- [Provider inventory](reference/provider-inventory.md): every known backend,
  shipped or not, and why.
- [Troubleshooting](reference/troubleshooting.md): common errors and what to
  do about them.
