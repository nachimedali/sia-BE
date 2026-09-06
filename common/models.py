"""Infrastructure tables (P0-43).

One model, and it exists only for the case where Redis is unreachable. The rate
budget lives in Redis because a token bucket needs atomic check-and-consume at
publish latency; this is the floor underneath it, sized so that "the limiter is
blind" degrades to *conservative*, never to *unlimited*.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.utils import timezone

#: The coarse window the fallback counts in. An hour because that is the unit
#: the provider's own cap is expressed in (25 posts/hour), so a whole-window
#: allowance maps onto it without arithmetic nobody can check.
WINDOW = dt.timedelta(hours=1)


class RateBudgetWindow(models.Model):
    """A per-`(provider, connection, window)` consumption counter (C-06).

    Deliberately cruder than the token bucket it stands in for: no refill
    curve, no reserve, no fractional tokens. Its only job is to keep publishing
    inside the provider's cap for as long as Redis is away, and a simple
    counter is something an operator can reason about at 3am.

    Rows are disposable — a nightly sweep can drop anything older than a day —
    which is why this is not append-only despite counting things.
    """

    provider = models.CharField(max_length=32)
    connection = models.CharField(max_length=128)
    window_start = models.DateTimeField()
    consumed_publish = models.PositiveIntegerField(default=0)
    consumed_ingest = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["provider", "connection", "window_start"],
                name="unique_rate_budget_window",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["window_start"])]

    def __str__(self) -> str:
        return f"{self.provider}/{self.connection} @ {self.window_start:%Y-%m-%d %H:00}"

    @staticmethod
    def current_window(now: dt.datetime | None = None) -> dt.datetime:
        moment = now or timezone.now()
        return moment.replace(minute=0, second=0, microsecond=0)

    @classmethod
    def consume(
        cls,
        *,
        provider: str,
        connection: str,
        limit: int,
        tokens: float = 1,
        now: dt.datetime | None = None,
    ) -> bool:
        """Take `tokens` from this window if the limit allows. Returns whether
        it was granted.

        `select_for_update` rather than an atomic `F()` increment because the
        decision depends on the value read — an unconditional increment would
        let concurrent callers jointly exceed the cap and only notice
        afterwards, which is the failure this table exists to prevent.
        """
        window = cls.current_window(now)
        wanted = max(1, int(tokens))

        try:
            with transaction.atomic():
                row, _ = cls.objects.get_or_create(
                    provider=provider, connection=connection, window_start=window
                )
                locked = cls.objects.select_for_update().get(pk=row.pk)
                if locked.consumed_publish + wanted > limit:
                    return False
                cls.objects.filter(pk=locked.pk).update(
                    consumed_publish=F("consumed_publish") + wanted
                )
                return True
        except IntegrityError:
            # Another worker created the same window between the get and the
            # create. Refuse this one rather than retry: the caller is a
            # publish that will be retried anyway, and a race here means the
            # window is busy, which is exactly when caution is right.
            return False
