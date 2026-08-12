"""Runs the due-publish scan once, on demand.

The third instance of the same gap `billing.grant_due_allowances` and
`reminders.send_due_reminders` already fill: nothing plays Celery Beat's role
under `playwright.config.ts`'s `webServer`, so an E2E flow that schedules an
auto-publish post would wait forever for a scan that never runs. Publishing is
idempotent per target (`PostTarget.idempotency_key`, I9), so calling it here is
the same operation Beat would have performed, just triggered by hand.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from scheduling import publishing


class Command(BaseCommand):
    help = "Publishes every auto-publish post that is due right now."

    def handle(self, *args: Any, **options: Any) -> None:
        attempted = publishing.publish_due()
        self.stdout.write(self.style.SUCCESS(f"Published {attempted} due post(s)."))
