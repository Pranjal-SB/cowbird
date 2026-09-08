"""Live probe of every registered provider.

The mocked suite proves this repo is internally consistent. Only this catches a
backend changing its API underneath us, which is the failure every library of
this kind eventually dies of.

Probe depth is deliberately shallow: `generate()` then `list()`, not `get()`
(a body read) or `delete()`. A provider whose body read or delete is broken
still reports "ok". This is a considered trade, not an oversight: one provider
in the fleet has a first `get()` of roughly 39 seconds, and probing it on every
provider on every run would make the canary that much slower per run.

Depth is covered elsewhere. `packages/core/tests/test_delivery.py` sends a real
message to every installed provider and reads the code back out of it, which
exercises `get()`, the HTML-to-text path and OTP extraction. Read "ok" here as
"answers the phone", and that test as "does the job".
"""

from __future__ import annotations

from cowbird.health import HealthStore, Status
from cowbird.provider import GenerateOptions
from cowbird.registry import Registry

_OUTCOME = {
    Status.OK: "ok",
    Status.SLOW: "ok",
    Status.DOWN: "down",
    Status.QUARANTINED: "quarantined",
}


async def run_canary(registry: Registry, health: HealthStore) -> dict[str, str]:
    """Acquire an address from every provider and record what happened.

    Providers are probed independently: one backend being down must never stop
    the others from being measured.
    """
    results: dict[str, str] = {}
    for provider in registry.all():
        try:
            address = await provider.generate(GenerateOptions())
            await provider.list(address)
        except Exception:
            # HealthTracked already recorded it. The canary only reports.
            pass
        results[provider.name] = _OUTCOME[health.status(provider.name)]
    return results
