from __future__ import annotations

import time
from collections.abc import Callable
from importlib.metadata import entry_points

from cowbird.errors import CowbirdError, NoProviderAvailable
from cowbird.health import HealthStore
from cowbird.provider import Provider
from cowbird.transport import Transport

ENTRY_POINT_GROUP = "cowbird.providers"


class HealthTracked(Provider):
    """Wraps a provider so every call it makes is timed and recorded.

    Without this, health is only ever observed at generate() time — which is the
    one call a rotted provider is least likely to fail. A backend whose list()
    silently changed shape would keep generating addresses happily and stay
    green forever, and rot detection is the entire point of the project.

    `watch` is deliberately NOT overridden with tracking logic beyond a plain
    passthrough: the contract suite (a later task) detects push support via
    `type(provider).watch is not Provider.watch`, and this wrapper must not
    make every wrapped provider look like it has push. `_inner` is exposed so
    that suite can unwrap to the real provider and test the real thing.
    """

    def __init__(self, inner: Provider, health: HealthStore) -> None:
        super().__init__(inner.http)
        self._inner = inner
        self._health = health
        self.name = inner.name
        self.caps = inner.caps

    async def _tracked(self, op: str, coro):
        started = time.monotonic()
        try:
            result = await coro
        except CowbirdError as exc:
            self._health.record_failure(self.name, exc)
            raise
        self._health.record_success(self.name, op, time.monotonic() - started)
        return result

    async def generate(self, opts=None):
        return await self._tracked("generate", self._inner.generate(opts))

    async def list(self, address):
        return await self._tracked("list", self._inner.list(address))

    async def get(self, address, id):
        return await self._tracked("get", self._inner.get(address, id))

    async def delete(self, address, id):
        return await self._tracked("delete", self._inner.delete(address, id))

    async def watch(self, address, poll=None):
        async for message in self._inner.watch(address, poll=poll):
            yield message


class Registry:
    """Finds providers. Does not choose between them — that is the Pool's job.

    Discovery is by entry point rather than import, so a provider package is
    installable, versionable, and releasable on its own without core holding a
    list of every provider that exists.
    """

    def __init__(
        self,
        transport_factory: Callable[[str], Transport] | None = None,
        discover: bool = True,
        health: HealthStore | None = None,
    ) -> None:
        self._health = health
        self._classes: dict[str, type[Provider]] = {}
        self._instances: dict[str, Provider] = {}
        # discover=False builds a registry holding only what is registered by
        # hand. Tests need it: once any provider package is installed in the
        # workspace, entry-point discovery would inject it into every registry
        # and quietly break assertions about which providers exist.
        self._discovered = not discover
        self._transport_factory = transport_factory or self._default_transport_factory

    def _default_transport_factory(self, name: str) -> Transport:
        # A provider class declares its own tolerance via caps.max_concurrency
        # (mail.tm's 8 QPS, e.g.). Ignoring it and always using Transport's
        # default of 4 would let callers fire more concurrent requests than
        # the backend accepts and collect rate-limit errors for it.
        cls = self._classes.get(name)
        max_concurrency = cls.caps.max_concurrency if cls is not None else 4
        return Transport(name, max_concurrency=max_concurrency)

    def register(self, cls: type[Provider]) -> None:
        self._classes[cls.name] = cls

    def discover(self) -> None:
        if self._discovered:
            return
        self._discovered = True
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            self.register(ep.load())

    def get(self, name: str) -> Provider:
        self.discover()
        if name in self._instances:
            return self._instances[name]
        cls = self._classes.get(name)
        if cls is None:
            raise NoProviderAvailable(tried=[name])
        instance = cls(self._transport_factory(name))
        if self._health is not None:
            instance = HealthTracked(instance, self._health)
        self._instances[name] = instance
        return instance

    def all(self) -> list[Provider]:
        self.discover()
        return [self.get(name) for name in sorted(self._classes)]

    async def aclose(self) -> None:
        for provider in self._instances.values():
            if provider.http is not None:
                await provider.http.aclose()
