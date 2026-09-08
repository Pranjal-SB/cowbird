from __future__ import annotations

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
    # See the spec's "Server build order" section; the real ceiling gets
    # measured against a deployed worker in Task 10.
    wait_max: int = 25
    wait_default: int = 25
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
