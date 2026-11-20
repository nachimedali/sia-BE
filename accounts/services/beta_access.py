"""Beta access requests from the public site (launch month).

One row per address; a repeat updates the row and counts the submission, and a
field left blank on the repeat keeps what the first submission said. Staff are
emailed every submission, queued after commit, never inline.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F

from accounts.models import BetaAccessRequest
from accounts.services.auth import queue_email

OPTIONAL = ("business_name", "phone", "social_url", "currency")


def record(*, email: str, plan: Any, **optional: str) -> BetaAccessRequest:
    email = email.strip().lower()
    filled = {k: v.strip() for k, v in optional.items() if k in OPTIONAL and v and v.strip()}
    with transaction.atomic():
        row, created = BetaAccessRequest.objects.select_for_update().get_or_create(
            email=email, defaults={"plan": plan, **filled}
        )
        if not created:
            for key, value in filled.items():
                setattr(row, key, value)
            row.plan = plan
            row.submissions = F("submissions") + 1
            row.save()
            row.refresh_from_db()
        transaction.on_commit(lambda: _notify(row))
    return row


def _notify(row: BetaAccessRequest) -> None:
    to = getattr(settings, "BETA_ACCESS_NOTIFY_EMAIL", "")
    if not to:
        return
    queue_email(
        to=to,
        subject=f"Beta access request: {row.email} ({row.plan.display_name})",
        template="beta_access_request",
        context={
            "email": row.email,
            "package": row.plan.display_name,
            "business_name": row.business_name,
            "phone": row.phone,
            "social_url": row.social_url,
            "currency": row.currency,
            "submissions": row.submissions,
        },
    )
