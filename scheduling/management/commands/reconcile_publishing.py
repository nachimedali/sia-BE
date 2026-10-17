"""Runs the publish reconciler once, on demand (SA-32).

What the `scheduling.tasks.publish_reconcile` beat job does every two minutes:
read the outcome of publishes the provider accepted, delete drafts that will
never go out, free provider media. `--orphans` also sweeps drafts a crash left
at the provider. Used when no beat runs (e2e, the live gate SA-90) and to see
at once where a stuck post stands.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from scheduling import reconcile


class Command(BaseCommand):
    help = "Reads pending publish outcomes, sweeps abandoned drafts and frees provider media."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--orphans", action="store_true", help="Also delete orphan drafts on our brands."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        counts = reconcile.run()
        if options["orphans"]:
            counts["orphan_drafts_discarded"] = reconcile.sweep_orphan_drafts()
        summary = ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in counts.items())
        self.stdout.write(self.style.SUCCESS(summary))
