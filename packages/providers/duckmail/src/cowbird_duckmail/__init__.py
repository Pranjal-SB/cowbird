"""DuckMail (api.duckmail.sbs), the backend behind freetempmail.com.

A mail.tm clone: same Hydra API, same account/token/messages flow. The one
protocol difference is that its list endpoints keep the `hydra:member`
envelope even under Accept: application/json, where mail.tm returns a bare
array.

All 19 domains it lists (checked 2026-09-24) carry only `isVerified: true`,
with no isPrivate/owner flag, and accounts were created anonymously on both
the first and the last of them, so no domain filtering is needed.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from cowbird.errors import SchemaDrift
from cowbird_mailtm import MailTm


class DuckMail(MailTm):
    name = "duckmail"
    api = "https://api.duckmail.sbs"
    caps = replace(
        MailTm.caps,
        sites=("duckmail.sbs", "freetempmail.com"),
        domain_count=19,
        # POST /accounts answers with expiresAt one day out.
        address_ttl=timedelta(days=1),
        # Not measured: no mail was sent. Nothing outlives its account.
        message_ttl=timedelta(days=1),
    )

    def _rows(self, payload: object, where: str) -> list:
        rows = payload.get("hydra:member") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            got = type(payload).__name__
            raise SchemaDrift(self.name, expected=f"hydra:member from {where}", got=got)
        return rows
