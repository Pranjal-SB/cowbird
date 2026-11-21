# Run the server

`cowbird-server` is the HTTP API, a FastAPI app in `packages/server`. Every
setting is an environment variable; [Settings](../reference/settings.md) lists
them all.

## With uvicorn

```bash
API_KEYS=local-dev uv run uvicorn cowbird_server:create_app --factory --host 127.0.0.1 --port 8000
```

`API_KEYS` is a comma-separated list. Every route except `/health` needs one of
them in an `x-api-key` header. Use `--host 0.0.0.0` only when the server should
be reachable from other machines.

## With Docker

```bash
docker build -t cowbird-server .
docker run --rm -e API_KEYS=local-dev -p 8000:8000 cowbird-server
```

The container listens on `$PORT`, 8000 by default, so hosts that assign a port
(Render, Koyeb, Fly) need no extra setup. It runs as a non-root user and has a
`HEALTHCHECK` against `/health`.

## What survives a restart

- **Issued addresses** live in process memory unless `DATABASE_URL` points at
  Postgres. Without it, a restart forgets every address, and later calls for
  them get `404 unknown address`.
- **Quarantines** are shared through Postgres when it is set.
- **Pending webhooks** always live in process memory. A restart drops them.
  Fine for "tell me when the signup mail lands", not for anything that must
  survive a redeploy.
- **Latency and health** are kept per instance on purpose: each instance
  measures from its own network.

## Several instances

Set the same `DATABASE_URL` on each instance and give each a stable, unique
`INSTANCE_ID`. A load balancer can then send a read to a different instance
than the one that issued the address.

`compose.yaml` runs this setup locally: Postgres, two servers, Caddy
round-robining between them on port 8000, and an optional Cloudflare solver.

```bash
docker compose up -d --build
curl -s -X POST -H "x-api-key: local-dev" http://127.0.0.1:8000/v1/inboxes
```

The two servers are also published on 8001 and 8002, so you can issue an
address on one and read it on the other.

**Keep the compose stack on your own machine.** Every port is bound to
127.0.0.1 because the stack uses a fixed API key and database password and
turns off the webhook address check (`WEBHOOK_ALLOW_PRIVATE`). Do not change
those bindings or deploy this file as it is.

## Webhooks in production

Leave `WEBHOOK_ALLOW_PRIVATE` off. With it off, the server refuses webhook URLs
that point at private, loopback or link-local addresses, so a caller cannot use
your server to reach your internal network. Set `WEBHOOK_SECRET` so receivers
can check deliveries came from you.
