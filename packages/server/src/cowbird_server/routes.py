from __future__ import annotations

from datetime import timedelta

from cowbird.models import Kind
from cowbird.pool import Pool
from cowbird.pool import Request as PoolRequest
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from cowbird_server.auth import require_api_key
from cowbird_server.envelope import envelope
from cowbird_server.service import InboxService

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


def _seconds(value: int | None) -> timedelta | None:
    return None if value is None else timedelta(seconds=value)


class CreateInbox(BaseModel):
    """Every field maps to a Pool Request field of the same name."""

    provider: str | None = None
    kind: Kind | None = None
    local: str | None = None
    domain: str | None = None
    domain_not_in: tuple[str, ...] = ()
    address_ttl_seconds: int | None = Field(default=None, ge=1)
    message_ttl_seconds: int | None = Field(default=None, ge=1)

    def to_request(self) -> PoolRequest:
        return PoolRequest(
            provider=self.provider,
            kind=self.kind,
            local=self.local,
            domain=self.domain,
            domain_not_in=self.domain_not_in,
            address_ttl=_seconds(self.address_ttl_seconds),
            message_ttl=_seconds(self.message_ttl_seconds),
        )


def get_service(request: Request) -> InboxService:
    return request.app.state.service


@router.post("/inboxes")
async def create_inbox(
    body: CreateInbox | None = None,
    service: InboxService = Depends(get_service),
):
    address = await service.create((body or CreateInbox()).to_request())
    # `state` is deliberately absent. It is a provider credential the server
    # holds so the client never has to.
    return envelope(
        data={
            "address": address.value,
            "provider": address.provider,
            "expires_at": address.expires_at.isoformat() if address.expires_at else None,
        }
    )


@router.get("/inboxes/{addr}/messages")
async def list_messages(addr: str, service: InboxService = Depends(get_service)):
    box = await service.inbox(addr)
    return envelope(
        data=[
            {
                "id": row.id,
                "sender": row.sender,
                "subject": row.subject,
                "received_at": row.received_at.isoformat() if row.received_at else None,
                "locked": row.locked,
            }
            for row in await box.messages()
        ]
    )
