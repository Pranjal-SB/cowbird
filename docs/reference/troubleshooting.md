# Troubleshooting

## `no provider available for that request`

Every provider that matches your filters is `down`, quarantined, or needs a
Cloudflare solver you have not set.

- Run `uv run cowbird providers` to see which are working.
- Relax the filters. `kind="gmail-alias"` alone narrows the choice to a few
  providers.
- A `down` provider is retried after 60 seconds, so trying again a minute
  later can work.

## A provider shows `down` from a server but works on your laptop

Cloudflare challenges many data-centre IP addresses that it lets home
connections through. cowbird records the challenge as `down` and routes around
it. Running a [Cloudflare solver](../guides/cloudflare-solver.md) on the same
network fixes it for the providers it covers.

## A provider shows `quarantined`

The service changed its responses and the adapter no longer understands them.
cowbird will not route to it until the adapter is fixed and the quarantine is
cleared; see [Health and quarantine](../concepts/health-and-quarantine.md).
Please open an issue with the output of `uv run cowbird canary --json`.

## `cowbird wait` says the provider requires `--state`

The provider issued a password or session with the address. Get it from
`uv run cowbird new --json` and pass it as `--state`.

## `404 unknown address` from the server

The server did not issue that address, or it restarted and kept addresses only
in memory. Set `DATABASE_URL` so addresses survive restarts; see
[Run the server](../guides/run-the-server.md).

## The code never arrives

- Check the site actually sent it: some sites refuse known throwaway domains
  without saying so. Ask for `kind="gmail-alias"`, or use `domain_not_in`.
- Some services drop mail from some senders. Try another provider with
  `provider=`.
- `wait` over HTTP returns `data: null` after `WAIT_MAX` seconds. That is not
  a failure; ask again.

## Live tests skip

The delivery test skips when testemailsender.com's daily limit is used up, or
when a provider's mail server refuses it. A skip is not a failure. See
[Test live delivery](../guides/test-live-delivery.md).
