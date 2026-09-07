from __future__ import annotations

from typing import Any


def envelope(data: Any = None, error: str | None = None) -> dict[str, Any]:
    """The one response shape. Every route and every error handler returns this.

    `success` is derived from `error` rather than passed in, so a caller cannot
    produce a body that claims success while carrying an error.
    """
    return {"success": error is None, "data": data, "error": error}
