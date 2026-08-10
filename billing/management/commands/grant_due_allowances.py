"""Runs the monthly-grant sweep once, on demand.

`billing.tasks.grant_monthly_allowances` normally runs from Celery Beat
(design.md §5), but nothing plays Beat's role in the E2E environment
(`playwright.config.ts`'s `webServer` only runs Django itself) — so a
freshly provisioned E2E workspace would otherwise carry a zero credit
balance forever, and any flow that spends credits (Studio, autopilot, tools)
would have nothing to test against. `grant_due_period_allowances` is
idempotent by construction (billing/services/subscriptions.py), so calling
it here is exactly the same operation Beat would have performed, just
triggered manually instead of on a schedule.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from billing.services.subscriptions import grant_due_period_allowances


class Command(BaseCommand):
    help = "Grants monthly allowances to every workspace due for one right now."

    def handle(self, *args: Any, **options: Any) -> None:
        granted = grant_due_period_allowances()
        self.stdout.write(self.style.SUCCESS(f"Granted allowances to {granted} workspace(s)."))
