from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from cowbird.errors import CowbirdError
from cowbird.health import HealthStore, Status
from cowbird.inbox import Inbox, aclose_default_pool, default_pool
from cowbird.models import Address, Kind
from cowbird.pool import Request

from cowbird_cli.canary import run_canary


def health_path() -> Path:
    """Where the CLI caches measured provider health between runs."""
    override = os.environ.get("COWBIRD_HEALTH_PATH")
    if override:
        return Path(override)
    return Path.home() / ".cowbird" / "health.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cowbird", description="disposable inboxes")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="acquire an address")
    new.add_argument("--provider")
    new.add_argument("--gmail", action="store_true", help="require a Gmail alias")
    new.add_argument("--json", action="store_true")

    wait = sub.add_parser("wait", help="wait for mail at an address")
    wait.add_argument("address")
    wait.add_argument("--provider", required=True)
    # Some providers (mail.tm) bind an inbox to a credential issued at generate
    # time. `cowbird new` is a separate process from `cowbird wait`, so that
    # credential has to travel between them or the second command cannot read
    # the first one's inbox at all.
    wait.add_argument("--state", default=None, help="opaque provider state from `new`")
    wait.add_argument("--otp", action="store_true")
    wait.add_argument("--timeout", type=float, default=120)

    sub.add_parser("providers", help="show the provider health matrix")
    canary = sub.add_parser("canary", help="probe every provider live and show health")
    canary.add_argument("--json", action="store_true")
    return parser


async def _new(args: argparse.Namespace) -> int:
    req = Request(
        provider=args.provider,
        kind=Kind.GMAIL_ALIAS if args.gmail else None,
    )
    provider, address = await default_pool().acquire(req)
    ttl = provider.caps.address_ttl
    if args.json:
        print(
            json.dumps(
                {
                    "address": address.value,
                    "provider": provider.name,
                    "address_ttl": str(ttl) if ttl else None,
                    # Emitted so `cowbird wait --state` can reach this inbox
                    # from another process. Treat it as a credential.
                    "state": address.state,
                }
            )
        )
    else:
        # Never print address.state here: it may carry provider credentials
        # (mail.tm packs a password into it) and this path is not JSON.
        print(f"{address.value}\t{provider.name}\tttl {ttl or 'forever'}")
    return 0


async def _wait(args: argparse.Namespace) -> int:
    provider = default_pool().registry.get(args.provider)
    if provider.caps.needs_state and not args.state:
        print(
            f"error: {args.provider} requires the --state value printed by "
            "`cowbird new --json`",
            file=sys.stderr,
        )
        return 1
    box = Inbox(provider, Address(args.address, args.provider, state=args.state))
    if args.otp:
        print(await box.otp(timeout=args.timeout))
    else:
        async for message in box.watch():
            print(f"{message.sender}\t{message.subject}")
            break
    return 0


async def _providers(args: argparse.Namespace) -> int:
    pool = default_pool()
    print(f"{'PROVIDER':<16}{'STATUS':<14}{'P50':<9}{'KIND':<26}SITES")
    for provider in pool.registry.all():
        p50 = pool.health.p50(provider.name)
        print(
            f"{provider.name:<16}"
            f"{pool.health.status(provider.name):<14}"
            # `is not None`, not truthiness: a p50 of 0.0 is a real measurement.
            # inboxes generates addresses without any HTTP call, so its median
            # is legitimately zero, and `if p50` printed that as "no data".
            f"{(f'{p50:.1f}s' if p50 is not None else '-'):<9}"
            f"{','.join(sorted(provider.caps.kind)):<26}"
            f"{','.join(provider.caps.sites)}"
        )
    return 0


async def _canary(args: argparse.Namespace) -> int:
    pool = default_pool()
    outcomes = await run_canary(pool.registry, pool.health)
    if args.json:
        # Carry last_failure as well. The tab-separated form says a provider is
        # quarantined but not why, so the CI issue body arrives with a name and
        # nothing anyone can act on.
        health = pool.health.snapshot()
        entries = {
            name: {
                "status": outcomes[name],
                "detail": health[name].last_failure if name in health else None,
            }
            for name in sorted(outcomes)
        }
        print(json.dumps(entries, indent=2))
    else:
        for name in sorted(outcomes):
            print(f"{name}\t{outcomes[name]}")
    # "down" alone must not fail the build: a backend being unreachable, or a
    # datacenter IP drawing a Cloudflare challenge, is not a defect here.
    # Only "quarantined" means an adapter is wrong and needs a human, so that
    # is the only outcome that turns the build red.
    return 1 if Status.QUARANTINED.value in outcomes.values() else 0


async def _run(args: argparse.Namespace) -> int:
    # Seed the live store the registry already holds a reference to, rather
    # than replacing it, so measurements taken during this command land
    # somewhere the next run's `providers`/routing can actually see.
    pool = default_pool()
    for name, entry in HealthStore.load(health_path()).snapshot().items():
        pool.health._entries[name] = entry
    handler = {
        "new": _new,
        "wait": _wait,
        "providers": _providers,
        "canary": _canary,
    }[args.command]
    try:
        return await handler(args)
    except CowbirdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        return 2
    finally:
        # A read-only or full disk must not fail the user's command.
        with contextlib.suppress(OSError):
            default_pool().health.save(health_path())
        # Closes curl_cffi sessions held by the shared pool/registry. Done
        # once here rather than per-command so every exit path (success,
        # CowbirdError, TimeoutError) tears the pool down the same way.
        await aclose_default_pool()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


def run() -> None:
    raise SystemExit(main())
