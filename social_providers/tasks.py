"""Housekeeping for the provider call log (`metrics_q`, like the other prunes)."""

from __future__ import annotations

import datetime as dt

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from social_providers.models import ProviderCall


@shared_task(name="social_providers.tasks.prune_provider_calls")
def prune_provider_calls() -> int:
    """Delete call-log rows past the cockpit's window. Operational data, not
    an audit trail — the audit trail is `AuditLog` and the posts themselves."""
    cutoff = timezone.now() - dt.timedelta(days=int(settings.SOCIALAPI_CALL_RETENTION_DAYS))
    deleted, _ = ProviderCall.objects.filter(created_at__lt=cutoff).delete()
    return deleted
