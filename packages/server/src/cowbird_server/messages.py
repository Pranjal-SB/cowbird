from __future__ import annotations

from cowbird.models import Message

__all__ = ["message_dict"]


def message_dict(message: Message, otp: str | None) -> dict:
    """The one shape a Message takes everywhere it crosses the wire: GET
    .../messages/{id}, GET .../wait, and the webhook payload. Each used to
    build this by hand and disagreed on which fields to include (`wait`
    dropped `html`, the webhook payload dropped `html` and `received_at`).
    `html` is included everywhere on purpose, for one parser to work
    against all three. `otp` is always the caller's already-computed value
    -- this does not call into cowbird.parsing itself, since wait's pattern
    and want_otp change what "the code" means.
    """
    return {
        "id": message.id,
        "sender": message.sender,
        "subject": message.subject,
        "received_at": message.received_at.isoformat() if message.received_at else None,
        "html": message.html,
        "text": message.text,
        "links": list(message.links),
        "otp": otp,
    }
