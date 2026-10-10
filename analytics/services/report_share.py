"""Sending a report to somebody with no account (P6-06).

**The Phase 2 `GUEST_VIEW` pattern, reused rather than reinvented.** The shape
is already correct and already tested in `collaboration.review`: minted at send,
only the digest persisted, expiring, revocable, and resolved by one lookup that
*is* the access control. A second credential with subtly different rules is how
one of the two ends up without a TTL.

Two deliberate differences from a review link, both following from what is
being shared:

*It points at a **run**, not a report.* A run is one rendering over one window,
frozen. Sharing the definition would mean the client's link showed different
numbers every time they opened it — and a figure somebody quoted in a meeting
must still say what it said.

*There is no approve path.* A report is read. Nothing a guest can do here
changes anything inside the tenant, which is what makes a 30-day multi-use link
acceptable at all.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from analytics.models import ReportRun, ReportRunStatus, ReportShareLink
from common.mail import Email, get_mail_sender
from common.tokens import digest as token_digest
from common.tokens import mint as mint_token

if TYPE_CHECKING:
    from accounts.models import User

#: Same 30 days as `GUEST_VIEW`. One number, one meaning: "a link a client can
#: keep open for a month".
TTL = dt.timedelta(days=30)

ROUTE = "s"


@transaction.atomic
def issue(run: ReportRun, *, created_by: User, email: str = "") -> tuple[ReportShareLink, str]:
    """Mint a link to one rendered run, and email it if there is an address.

    Supersedes any outstanding link to the same run for the same address, for
    the reason re-sending a verification email does: leaving the earlier link
    live widens the window on one that may have gone to a mistyped address.

    **A run that is not `READY` cannot be shared.** A link to a pending render
    is a link to a blank page, and a link to a failed one is a link to nothing
    at all — both arrive looking like the product is broken.
    """
    if run.status != ReportRunStatus.READY:
        raise ValueError("Only a rendered report can be shared.")

    normalised = email.strip().lower()
    if normalised:
        ReportShareLink.objects.filter(run=run, email=normalised, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )

    raw, token_hash = mint_token()
    link = ReportShareLink.objects.create(
        run=run,
        email=normalised,
        created_by=created_by,
        token_hash=token_hash,
        expires_at=timezone.now() + TTL,
    )

    if normalised:
        url = f"{settings.SITE_URL}/{ROUTE}/{raw}"
        get_mail_sender().send(
            Email(
                to=normalised,
                subject=f"Your report: {run.report.name}",
                template="report_share",
                context={
                    "report": run.report,
                    "run": run,
                    "report_url": url,
                    "workspace": run.report.workspace,
                },
            )
        )
    return link, raw


def resolve(raw: str) -> ReportShareLink | None:
    """The one lookup the public report view goes through.

    `None` for an unknown, expired or revoked token, indistinguishably — being
    able to tell them apart would confirm that a report exists behind a token
    somebody guessed.
    """
    link = (
        ReportShareLink.objects.select_related("run", "run__report", "run__report__workspace")
        .filter(token_hash=token_digest(raw))
        .first()
    )
    return link if link is not None and link.is_usable else None


def revoke(link: ReportShareLink) -> ReportShareLink:
    """Idempotent: two people closing the same link at once is a race, not a
    conflict worth surfacing."""
    if link.revoked_at is None:
        link.revoked_at = timezone.now()
        link.save(update_fields=["revoked_at"])
    return link


def guest_context(link: ReportShareLink) -> dict[str, Any]:
    """What a guest is allowed to see: the frozen payload, and nothing else.

    Assembled from `run.payload` rather than recomputed, so the shared view is
    the same numbers as the PDF by construction. Recomputing would also read
    live querysets on a public endpoint, which is the shape a tenancy leak
    takes.
    """
    run = link.run
    return {
        "report_name": run.report.name,
        "workspace_name": run.report.workspace.name,
        "window_start": run.window_start,
        "window_end": run.window_end,
        "payload": run.payload,
        "document_url": run.document.url if run.document else "",
        "expires_at": link.expires_at,
    }
