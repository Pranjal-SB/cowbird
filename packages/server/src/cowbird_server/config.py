from __future__ import annotations

import os
import socket
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _csv(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return value
    return [item.strip() for item in value.split(",") if item.strip()]


# NoDecode stops pydantic-settings from JSON-parsing the raw env string for
# list fields, so the validator below can split plain CSV instead.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, case_sensitive=False
    )

    api_keys: CsvList = Field(default=[], validation_alias=AliasChoices("api_keys", "API_KEYS"))
    allowed_origins: CsvList = Field(
        default=[], validation_alias=AliasChoices("allowed_origins", "ALLOWED_ORIGINS")
    )
    rate_limit: str = "30/minute"
    # Well under Cloudflare's ceiling on a proxied subrequest. The client
    # re-issues rather than holding one long request open through the edge.
    # The real ceiling has not been measured against a deployed worker yet.
    wait_max: int = 25
    wait_default: int = 25
    # A webhook holds no request open -- the caller gets an id back and the wait
    # happens server-side -- so Cloudflare's ceiling on a proxied subrequest,
    # which is what WAIT_MAX is for, does not apply to it. Long enough for "tell
    # me when the signup mail lands", short enough that a redeploy rarely lands
    # on a live registration, which is the exposure of keeping these in process.
    webhook_max: int = Field(default=600, ge=1)
    # Unset selects MemoryStore and changes nothing. That is the single-instance
    # deploy and the whole existing test suite.
    database_url: str | None = None
    # Key for this instance's own health rows. Must be stable across restarts or
    # the instance never finds its own latency history again, and unique per
    # process: the one-writer-per-row guarantee is instance_id being in the
    # primary key, so `uvicorn --workers 4` under a bare hostname gives four
    # writers one row and starts applying one egress's residential-IP hint to
    # another. The pid keeps them apart when nobody sets this explicitly.
    instance_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    # ge=1 because 0 is a `while True` with no sleep: it hammers Postgres as
    # fast as the loop can issue queries and pins a core doing it.
    health_flush_seconds: int = Field(default=10, ge=1)
    webhook_secret: str | None = None
    webhook_allow_private: bool = False

    @field_validator("api_keys", "allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):
        return _csv(v)

    def clamp_wait(self, requested: int | None) -> int:
        """The one place the wait-timeout policy lives: default when unset,
        capped at wait_max either way."""
        return min(requested or self.wait_default, self.wait_max)

    def clamp_webhook(self, requested: int | None) -> int:
        """The webhook timeout policy, deliberately not clamp_wait.

        Sharing that method is how the two limits got conflated: a webhook was
        capped at the long-poll ceiling and could not outlive a single held
        request, which defeats the reason to register one.
        """
        return min(requested or self.webhook_max, self.webhook_max)


@lru_cache
def get_settings() -> Settings:
    return Settings()
