# Use a Cloudflare solver

Six providers sit behind Cloudflare Turnstile: smailpro (Gmail and Outlook),
cftempmail, xeramail, vanishinbox, zenvex and etempmail. cowbird skips them
until you give it a solver. Nothing else changes either way.

## Set it up

Run [cloudflare-solver](https://github.com/B00H0O/cloudflare-solver) and point
cowbird at it:

```bash
COWBIRD_SOLVER_URL=http://localhost:407
```

Put that in your environment or in `.env` at the repo root. `compose.yaml`
already runs a solver for its two servers.

## Two rules

**Never expose the solver.** It drives a browser to any URL it is handed.
Reachable from the network, it lets anyone make requests from your machine.
Bind it to loopback, as `compose.yaml` does.

**Run it on the same network exit as cowbird.** Cloudflare ties the tokens it
issues to the IP address that earned them. A solver on another machine earns
tokens that cowbird's requests cannot use.

## Tests

Tests that need the solver are marked `solver` and skip when
`COWBIRD_SOLVER_URL` is unset.
