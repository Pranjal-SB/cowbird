from cowbird.errors import ProviderDown, SchemaDrift
from cowbird.health import HealthStore, Status
from cowbird.registry import Registry
from cowbird.testing import CAPS, FakeProvider
from cowbird_cli.canary import run_canary


def provider_class(name, generate_error=None):
    ns = {"name": name, "caps": CAPS}
    if generate_error is not None:

        async def generate(self, opts=None):
            raise generate_error

        ns["generate"] = generate
    return type(name.upper(), (FakeProvider,), ns)


def build(*classes, health=None):
    reg = Registry(discover=False, health=health, transport_factory=lambda n: None)
    for cls in classes:
        reg.register(cls)
    return reg


async def test_canary_reports_a_healthy_provider_ok():
    health = HealthStore()
    reg = build(provider_class("good"), health=health)
    assert await run_canary(reg, health) == {"good": "ok"}


async def test_canary_marks_a_failing_provider_down():
    health = HealthStore()
    reg = build(provider_class("bad", ProviderDown("boom")), health=health)
    assert await run_canary(reg, health) == {"bad": "down"}
    assert health.status("bad") is Status.DOWN


async def test_canary_quarantines_on_schema_drift():
    health = HealthStore()
    drift = SchemaDrift("drifted", expected="hydra:member", got=["items"])
    reg = build(provider_class("drifted", drift), health=health)
    assert await run_canary(reg, health) == {"drifted": "quarantined"}
    assert health.status("drifted") is Status.QUARANTINED


async def test_one_broken_provider_does_not_stop_the_others():
    health = HealthStore()
    reg = build(
        provider_class("bad", ProviderDown("boom")),
        provider_class("good"),
        health=health,
    )
    assert await run_canary(reg, health) == {"bad": "down", "good": "ok"}


async def test_canary_records_the_drift_detail_for_the_issue_body():
    health = HealthStore()
    drift = SchemaDrift("drifted", expected="hydra:member", got=["items", "total"])
    reg = build(provider_class("drifted", drift), health=health)
    await run_canary(reg, health)
    failure = health.snapshot()["drifted"].last_failure or ""
    assert "hydra:member" in failure
    assert "items" in failure
