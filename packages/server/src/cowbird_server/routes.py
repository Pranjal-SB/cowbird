from __future__ import annotations

from datetime import timedelta

from cowbird.health import Status
from cowbird.models import Kind
from cowbird.pool import Pool
from cowbird.pool import Request as PoolRequest
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cowbird_server.auth import require_api_key
from cowbird_server.config import get_settings
from cowbird_server.envelope import envelope
from cowbird_server.messages import message_dict
from cowbird_server.service import InboxService
from cowbird_server.webhooks import WebhookManager

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


def get_db_pool(request: Request):
    return request.app.state.db_pool


@router.delete("/providers/{name}/quarantine")
async def clear_quarantine(
    name: str,
    pool: Pool = Depends(get_pool),
    db_pool=Depends(get_db_pool),
):
    """Return a quarantined provider to routing.

    Quarantine used to clear on restart, which was accidental but was the only
    way out. A durable global row removes that, so without this route a provider
    stays dead forever.

    The global row is deleted first. Clearing only the local status would leave
    the row in place for the next flush to read straight back, and the clear
    would look like it silently failed.
    """
    if name not in {p.name for p in pool.registry.all()}:
        raise HTTPException(status_code=404, detail="unknown provider")
    was = pool.health.status(name) is Status.QUARANTINED
    if db_pool is not None:
        async with db_pool.acquire() as conn:
            await conn.execute("delete from provider_quarantine where provider = $1", name)
    if was:
        pool.health.restore(name, status=Status.OK)
    return envelope(data={"cleared": was})


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


@router.get("/inboxes/{addr}/messages/{message_id}")
async def get_message(addr: str, message_id: str, service: InboxService = Depends(get_service)):
    message, otp = await service.get_message(addr, message_id)
    return envelope(data=message_dict(message, otp))


@router.delete("/inboxes/{addr}/messages/{message_id}")
async def delete_message(addr: str, message_id: str, service: InboxService = Depends(get_service)):
    box = await service.inbox(addr)
    await box.delete(message_id)
    return envelope(data={"deleted": message_id})


@router.get("/inboxes/{addr}/wait")
async def wait_for_mail(
    addr: str,
    timeout: int | None = Query(default=None, ge=1),
    otp: bool = Query(default=False),
    pattern: str | None = Query(default=None),
    service: InboxService = Depends(get_service),
):
    held = get_settings().clamp_wait(timeout)
    message, code = await service.wait(addr, held, pattern=pattern, want_otp=otp)
    if message is None:
        return envelope(data=None)
    return envelope(data=message_dict(message, code))


class CreateWebhook(BaseModel):
    address: str
    url: str
    timeout: int = Field(default=25, ge=1)


def get_webhooks(request: Request) -> WebhookManager:
    return request.app.state.webhooks


@router.post("/webhooks")
async def create_webhook(
    body: CreateWebhook,
    webhooks: WebhookManager = Depends(get_webhooks),
    service: InboxService = Depends(get_service),
):
    # Resolve first: WebhookManager.create is sync and cannot await
    # service.inbox() itself, so an address this server never issued would
    # otherwise register fine and only fail as a silent timeout later.
    # UnknownAddress from a bad address propagates to the existing 404 handler.
    await service.inbox(body.address)
    timeout = get_settings().clamp_webhook(body.timeout)
    try:
        hook_id = webhooks.create(body.address, body.url, timeout)
    except ValueError as exc:
        # The one place a raw exception message reaches a client, deliberately:
        # it is about the caller's own URL, which they already have.
        return JSONResponse(status_code=422, content=envelope(error=str(exc)))
    return envelope(data={"id": hook_id})


@router.get("/webhooks")
async def list_webhooks(webhooks: WebhookManager = Depends(get_webhooks)):
    return envelope(
        data=[
            {"id": h["id"], "address": h["address"], "url": h["url"], "timeout": h["timeout"]}
            for h in webhooks.list()
        ]
    )


@router.delete("/webhooks/{hook_id}")
async def delete_webhook(hook_id: str, webhooks: WebhookManager = Depends(get_webhooks)):
    return envelope(data={"cancelled": webhooks.cancel(hook_id)})
