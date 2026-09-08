from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from cowbird_server.config import get_settings


def _key_matches(candidate: str, keys: list[str]) -> bool:
    # Constant-time compare against each configured key, so a timing signal
    # cannot be used to recover one byte at a time.
    return any(secrets.compare_digest(candidate, k) for k in keys)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    keys = get_settings().api_keys
    if not x_api_key or not _key_matches(x_api_key, keys):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return x_api_key
