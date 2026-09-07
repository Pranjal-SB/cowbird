from __future__ import annotations

from cowbird.pool import Pool
from fastapi import APIRouter, Depends, Request

from cowbird_server.auth import require_api_key
from cowbird_server.envelope import envelope

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def get_pool(request: Request) -> Pool:
    return request.app.state.pool


@router.get("/providers")
async def providers(pool: Pool = Depends(get_pool)):
    """The health matrix: measured, not declared."""
    return envelope(
        data=[
            {
                "provider": provider.name,
                "status": str(pool.health.status(provider.name)),
                "p50": pool.health.p50(provider.name),
                "kind": sorted(provider.caps.kind),
                "sites": list(provider.caps.sites),
            }
            for provider in sorted(pool.registry.all(), key=lambda p: p.name)
        ]
    )
