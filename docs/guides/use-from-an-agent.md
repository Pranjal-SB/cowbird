# Use cowbird from an agent

An agent signing up for something needs an address it can read and the code
the site sends back. cowbird gives it both with output a program can parse and
failures it can tell apart. Pick the HTTP API when the agent already makes web
requests, the CLI when it runs shell commands, and the library when it is
Python code.

## The short version

Hand an agent these, and it has everything it needs.

```text
Get an address:   POST /v1/inboxes                        -> data.address
Wait for a code:  GET  /v1/inboxes/{address}/wait?otp=true -> data.otp (null: ask again)
Read a message:   GET  /v1/inboxes/{address}/messages/{id} -> data.text, data.links
Next message:     GET  /v1/inboxes/{address}/wait?skip={id1},{id2}
Every response:   {"success": bool, "data": ..., "error": string or null}
```

Or on the command line:

```text
uv run cowbird new --json                                  -> address, provider, state
uv run cowbird wait ADDRESS --provider P --state S --otp   -> the code on stdout
exit code 0 ok, 1 error, 2 timeout
```

## HTTP API

**One response shape.** Every route returns
`{"success": ..., "data": ..., "error": ...}`, errors included. An agent
checks `success` and reads `data` or `error`; it never parses an HTML error
page or a stack trace.

**Status codes say what to do next.**

| Status | `error` | What to do |
|---|---|---|
| 401 | invalid or missing API key | Fix the key. |
| 404 | unknown address | This server did not issue the address, or it restarted without Postgres. Get a new one. |
| 410 | that address has expired | Get a new address. |
| 410 | that message is no longer available | Skip the message. |
| 422 | invalid request: ... | Fix the request body or query. |
| 422 | no provider supports that combination of options | The provider cannot do that call. Change the request. |
| 423 | the message is behind the provider's paywall | Skip the message. |
| 429 | rate limit exceeded | Your API key hit `RATE_LIMIT`. Back off. |
| 429 | upstream rate limited | The provider is throttling. Back off and retry. |
| 502 | upstream provider error | Retry; cowbird routes around a failing provider. |
| 503 | no provider available for that request | No working provider matches. Relax the filters or retry later. |
| 503 | address store unavailable / cloudflare solver unavailable | A server dependency is down. Retry later. |
| 500 | internal server error | A bug. Retry once, then report it. |

**Waiting.** `GET .../wait` holds the request until mail arrives, for at most
`WAIT_MAX` seconds (25 by default) whatever `timeout` asks for. `data: null`
means nothing yet: send the same request again. This keeps each request short
enough to pass through proxies that cut long ones.

**A second message.** A message stays in the inbox after it is read. To wait
for the login code after the signup code, pass the ids already seen:
`?skip=m1,m2`.

**Not polling at all.** `POST /v1/webhooks` with `{"address": ..., "url": ...}`
makes the server wait and POST the message to `url` once, as
`{"event": "message", "address": ..., "otp": ..., ...}`, or
`{"event": "timeout", "address": ...}` if nothing came. Set `WEBHOOK_SECRET`
and each delivery carries `x-cowbird-signature: sha256=<hmac of the body>`.

**The server holds secrets.** Some providers issue a password or session with
the address. The HTTP API keeps it server-side; a client only ever sees the
address.

## CLI

`uv run cowbird new --json` prints one JSON object: `address`, `provider`,
`address_ttl` and `state`. Pass `provider` and `state` to `wait`; `state` may
carry a password, so keep it out of logs.

`uv run cowbird wait ... --otp` prints only the code, so the agent reads one
line from stdout. Exit codes: `0` success, `1` a cowbird error, `2` a timeout.
Messages go to stderr as `error: ...` or `timeout: ...`. argparse also exits
`2` on a bad flag, so check stderr to tell a typo from a timeout.

## Python

```python
async with cowbird.inbox() as box:
    address = box.address.value
    code = await box.otp(timeout=120)              # TimeoutError if none
    link = await box.link(match="verify", timeout=120)
```

`otp()` and `link()` each remember which messages they answered from, so a
second `otp()` returns the next code, not the first again. Errors are
subclasses of `cowbird.errors.CowbirdError`; see the
[library reference](../reference/library.md).

## Choosing what kind of address

Many signup forms refuse known throwaway domains. Ask for a Gmail or Outlook
alias with `kind="gmail-alias"` or `kind="outlook-alias"`, or exclude domains
the site blocks with `domain_not_in`. See
[Providers and routing](../concepts/providers-and-routing.md).
