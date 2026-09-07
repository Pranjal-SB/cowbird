"""One-shot outbound webhooks.

Registrations live in this process. A restart drops every pending hook without
telling the caller their hook is gone, which is the same defect the spec records
against emailnator-api. A later change moves them into Postgres alongside the address
store.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import urllib.parse
import uuid

from cowbird_server.service import InboxService

logger = logging.getLogger("cowbird.server")

_DELIVERY_RETRIES = 3
_DELIVERY_TIMEOUT = 15


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url(url: str, allow_private: bool, resolve=None) -> str:
    """Reject non-http(s) URLs and, unless allowed, any host that resolves to a
    private/loopback/link-local/reserved address — a literal IP *or* a domain
    name that resolves to one (SSRF guard).

    resolution happens here, but curl re-resolves at connect time, so
    a TOCTOU / DNS-rebinding race remains (host flips to an internal IP between
    check and connect). Closing it fully means pinning the validated IP for the
    actual connection; not worth it for a single-tenant self-host. Redirects are
    disabled at delivery so at least a redirect can't reach an internal target.
    """
    resolve = resolve or socket.getaddrinfo
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("url must be http or https")
    host = parsed.hostname
    if not host:
        raise ValueError("url missing host")
    if allow_private:
        return url
    try:
        infos = resolve(host, parsed.port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve webhook host: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise ValueError(
                "webhook host resolves to a private/loopback address "
                "(set WEBHOOK_ALLOW_PRIVATE=1 to override)"
            )
    return url


class WebhookManager:
    def __init__(
        self, service: InboxService, *, allow_private: bool, secret: str | None, session=None
    ):
        self._service = service
        self._allow_private = allow_private
        self._secret = secret
        self._session = session
        self._hooks: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def create(self, address: str, url: str, timeout: int) -> str:
        validate_url(url, self._allow_private)  # raises ValueError on a bad url
        hook_id = uuid.uuid4().hex[:12]
        self._hooks[hook_id] = {
            "id": hook_id,
            "address": address,
            "url": url,
            "timeout": timeout,
        }
        self._tasks[hook_id] = asyncio.create_task(self._run(hook_id))
        return hook_id

    def list(self) -> list[dict]:
        return list(self._hooks.values())

    def cancel(self, hook_id: str) -> bool:
        task = self._tasks.pop(hook_id, None)
        self._hooks.pop(hook_id, None)
        if task is None:
            return False
        task.cancel()
        return True

    async def _ensure_session(self):
        if self._session is None:
            from curl_cffi.requests import AsyncSession

            self._session = AsyncSession()
        return self._session

    async def _run(self, hook_id: str):
        hook = self._hooks[hook_id]
        try:
            message, code = await self._service.wait(hook["address"], hook["timeout"])
            if message is None:
                payload = {"event": "timeout", "address": hook["address"]}
            else:
                payload = {
                    "event": "message",
                    "address": hook["address"],
                    "id": message.id,
                    "sender": message.sender,
                    "subject": message.subject,
                    "text": message.text,
                    "links": list(message.links),
                    "otp": code,
                }
            await self._deliver(hook["url"], payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("webhook %s failed: %s", hook_id, exc)
        finally:
            self._hooks.pop(hook_id, None)
            self._tasks.pop(hook_id, None)

    async def _deliver(self, url: str, payload: dict):
        data = json.dumps(payload).encode()
        headers = {"content-type": "application/json"}
        if self._secret:
            sig = hmac.new(self._secret.encode(), data, hashlib.sha256).hexdigest()
            headers["x-cowbird-signature"] = f"sha256={sig}"
        session = await self._ensure_session()
        for attempt in range(_DELIVERY_RETRIES):
            try:
                resp = await session.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=_DELIVERY_TIMEOUT,
                    allow_redirects=False,  # a redirect must not reach an internal target
                )
                if resp.status_code < 400:
                    return
                logger.info(
                    "webhook delivery %s -> %s (attempt %s)", url, resp.status_code, attempt + 1
                )
            except Exception as exc:
                logger.info(
                    "webhook delivery error %s (attempt %s): %s", url, attempt + 1, exc
                )
            await asyncio.sleep(2 * (attempt + 1))
        logger.warning(
            "webhook delivery to %s failed after %s attempts", url, _DELIVERY_RETRIES
        )

    async def shutdown(self):
        for task in list(self._tasks.values()):
            task.cancel()
        self._hooks.clear()
        self._tasks.clear()
