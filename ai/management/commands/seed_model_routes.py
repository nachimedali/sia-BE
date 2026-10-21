"""Seeds the model routes (`ai/route_seed.py`).

`migrate` already does this (`ai/0021`); the command exists for the deliberate
case. Without flags it only creates missing rows. `--refresh` overwrites rows
with the seed, discarding admin edits, and records each overwrite as a route
change so the history says who reset what.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from ai.models import AIModelRoute, AIModelRouteChange
from ai.route_seed import FIELDS, ROUTES
from billing.models import Plan


class Command(BaseCommand):
    help = "Seed the model each task runs on, per package (AI → Model routes)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Overwrite existing routes with the seed (discards admin edits).",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        created = refreshed = skipped = 0
        for spec in ROUTES:
            plan = None
            if spec["plan"]:
                plan = Plan.objects.filter(code=spec["plan"]).first()
                if plan is None:
                    skipped += 1
                    continue
            defaults = {key: spec[key] for key in FIELDS}
            route, was_created = AIModelRoute.objects.get_or_create(
                task=spec["task"], plan=plan, defaults=defaults
            )
            if was_created:
                created += 1
                continue
            if not options["refresh"]:
                continue
            before = route.snapshot()
            for key, value in defaults.items():
                setattr(route, key, value)
            route.is_active = True
            route.save()
            if route.snapshot() != before:
                AIModelRouteChange.objects.create(
                    route=route, before=before, after=route.snapshot()
                )
                refreshed += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Model routes: {created} created, {refreshed} refreshed, "
                f"{skipped} skipped (package missing)."
            )
        )
