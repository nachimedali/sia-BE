"""Collect the Playbook now, instead of waiting for beat.

`--all` asks every active source whatever its cadence — what a fresh database
or a demo needs. Without it, only due sources run, exactly like the beat job.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from playbook.models import PlaybookSource
from playbook.services import collect, scoring


class Command(BaseCommand):
    help = "Collects due Playbook sources (or every source with --all) and rescores."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--all", action="store_true", help="Ignore each source's cadence.")

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["all"]:
            result = collect.collect_due()
            self.stdout.write(f"{result['sources']} sources, {result['posts']} posts.")
            return
        now = timezone.now()
        regions: set[str] = set()
        posts = 0
        sources = list(PlaybookSource.objects.filter(is_active=True).select_related("category"))
        for source in sources:
            posts += collect.collect_source(source, now=now)
            regions.add(source.region)
        for region in sorted(regions):
            scoring.rescore(region, now=now)
        self.stdout.write(f"{len(sources)} sources, {posts} posts.")
