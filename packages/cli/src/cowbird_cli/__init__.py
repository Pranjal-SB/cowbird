from __future__ import annotations

import argparse
import asyncio
import json
import sys

from cowbird.errors import CowbirdError
from cowbird.inbox import Inbox, aclose_default_pool, default_pool
from cowbird.models import Address, Kind
from cowbird.pool import Request


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
    if provider.caps.delete and not args.state:
        # No capability field says "needs the state issued by generate()"
        # directly. `delete` is the least-bad proxy available: a provider
        # that supports deleting mail is doing authenticated mutation
        # against the backend, which in every provider so far (mail.tm)
        # means it also needs a per-account session it can only get back
        # via the state `new --json` printed. Catching this here gives a
        # clear message instead of an adapter-specific one three calls deep.
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
            f"{(f'{p50:.1f}s' if p50 else '-'):<9}"
            f"{','.join(sorted(provider.caps.kind)):<26}"
            f"{','.join(provider.caps.sites)}"
        )
    return 0


async def _run(args: argparse.Namespace) -> int:
    handler = {"new": _new, "wait": _wait, "providers": _providers}[args.command]
    try:
        return await handler(args)
    except CowbirdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except TimeoutError as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        return 2
    finally:
        # Closes curl_cffi sessions held by the shared pool/registry. Done
        # once here rather than per-command so every exit path (success,
        # CowbirdError, TimeoutError) tears the pool down the same way.
        await aclose_default_pool()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))


def run() -> None:
    raise SystemExit(main())
