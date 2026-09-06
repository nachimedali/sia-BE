"""Organization add-ons (P0-19, P0-49).

Org-level, with independent 30-day trials, because an add-on is bought by the
company rather than by one brand — the same reason entitlement accounting pools
at the organization (L-1).

Every write goes through here so `bump_addon_version` cannot be forgotten:
that counter is half the entitlement cache key, and an add-on enabled without
bumping it stays invisible for up to five minutes, which reads to the customer
as "I paid and nothing happened".
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from billing.models import AddonStatus, OrganizationAddon
from billing.services.entitlements import bump_addon_version

logger = logging.getLogger(__name__)

#: Every add-on gets the same trial length. A per-add-on clock is a commercial
#: number and would belong on a row, but there is one number today and
#: inventing a table to hold it would be speculative.
TRIAL = dt.timedelta(days=30)


@transaction.atomic
def set_addon(organization: Any, *, key: str, enabled: bool, with_trial: bool = True) -> None:
    """Enables or disables one add-on, and invalidates the cache either way."""
    if enabled:
        OrganizationAddon.objects.update_or_create(
            organization=organization,
            addon_key=key,
            defaults={
                "status": AddonStatus.ACTIVE,
                "trial_ends_at": timezone.now() + TRIAL if with_trial else None,
            },
        )
    else:
        OrganizationAddon.objects.filter(organization=organization, addon_key=key).update(
            status=AddonStatus.CANCELLED, trial_ends_at=None
        )
    bump_addon_version(organization)
    logger.info(
        "organization add-on changed",
        extra={"organization_id": organization.pk, "addon": key, "enabled": enabled},
    )


def expire_addon_trials(now: dt.datetime | None = None) -> int:
    """Swept by the nightly `expire_trials` job at 02:45 (P0-19).

    A clock rather than a read-time check on purpose: an add-on that lapsed
    mid-request would make the same request answer differently at 02:44 and
    02:46, and a customer would see a feature blink out between two clicks.
    """
    moment = now or timezone.now()
    lapsed = OrganizationAddon.objects.filter(
        status=AddonStatus.ACTIVE, trial_ends_at__isnull=False, trial_ends_at__lte=moment
    )
    organizations = list(lapsed.values_list("organization", flat=True).distinct())
    expired = lapsed.update(status=AddonStatus.EXPIRED)

    if expired:
        from workspaces.models import Organization

        for organization in Organization.objects.filter(pk__in=organizations):
            bump_addon_version(organization)
        logger.info("organization add-on trials expired", extra={"count": expired})
    return int(expired)
