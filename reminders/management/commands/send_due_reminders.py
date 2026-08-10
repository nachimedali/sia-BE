"""Runs the reminder-send sweep once, on demand.

`reminders.tasks.send_due_reminders` normally runs from Celery Beat every
minute (design.md §5.1), but nothing plays Beat's role in the E2E environment
(`playwright.config.ts`'s `webServer` only runs Django itself) — the same gap
`billing.management.commands.grant_due_allowances` exists to close. Calling
`reminders.services.send_due` here is exactly what Beat would have done, just
triggered manually instead of on a schedule.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from reminders.services import send_due


class Command(BaseCommand):
    help = "Sends every reminder that is currently due."

    def handle(self, *args: Any, **options: Any) -> None:
        sent = send_due()
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} reminder(s)."))
