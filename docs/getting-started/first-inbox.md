# Your first inbox

The job: get an address, use it to sign up somewhere, and read the one-time
code the site sends. Here it is three ways.

## From Python

```python
import asyncio
import cowbird


async def main():
    async with cowbird.inbox() as box:
        print(box.address.value)          # sign up with this address
        code = await box.otp(timeout=120)
        print(code)


asyncio.run(main())
```

`cowbird.inbox()` picks the best working provider. `otp()` waits for a message
that holds a code and returns the code; after 120 seconds with no code it
raises `TimeoutError`. Want the confirmation link instead? Use
`await box.link(match="confirm")`.

When the `async with` block ends, cowbird deletes the messages it read, where
the provider allows it.

## From the command line

```bash
$ uv run cowbird new --json
{"address": "cb1f0a2e@example.test", "provider": "mailtm", "address_ttl": null, "state": "..."}
```

Sign up with the address, then wait for the code:

```bash
$ uv run cowbird wait cb1f0a2e@example.test --provider mailtm --state '...' --otp
482913
```

`wait` needs the `provider` from `new`, and the `state` too. `state` carries
whatever the provider needs to open that inbox again, such as a password, so
treat it as a secret. Without `--otp`, `wait` prints the sender and subject of
the first message.

## Over HTTP

Start the server (see [Run the server](../guides/run-the-server.md)):

```bash
API_KEYS=local-dev uv run uvicorn cowbird_server:create_app --factory --port 8000
```

Then:

```bash
$ curl -s -X POST -H "x-api-key: local-dev" localhost:8000/v1/inboxes
{"success":true,"data":{"address":"cb1f0a2e@example.test","provider":"mailtm","expires_at":null},"error":null}

$ curl -s -H "x-api-key: local-dev" \
    "localhost:8000/v1/inboxes/cb1f0a2e@example.test/wait?otp=true"
{"success":true,"data":{"id":"m1","subject":"Your code","otp":"482913",...},"error":null}
```

The server keeps the provider's `state` itself, so an HTTP client never sees
it. Each `wait` is held for at most 25 seconds; if `data` is `null`, nothing
arrived yet, so ask again.

## Narrowing the choice

All three take the same filters. For a Gmail alias:

- Python: `cowbird.inbox(kind="gmail-alias")`
- CLI: `uv run cowbird new --gmail`
- HTTP: `POST /v1/inboxes` with `{"kind": "gmail-alias"}`

[Providers and routing](../concepts/providers-and-routing.md) lists every
filter.
