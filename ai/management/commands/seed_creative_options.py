"""Seeds the Studio's controls (`ai.creative_seed`).

Idempotent, and **never clobbers an edit**: a row that exists is left alone, so
a label or an icon an operator retuned in admin survives the next deploy's
re-seed. `--refresh` is the deliberate overwrite, for when the seed itself has
improved and the operator wants it.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction

from ai.ad_template_seed import AD_TEMPLATE_CATALOG
from ai.creative_i18n import translations_for
from ai.creative_seed import CATALOG
from ai.models import CreativeOption
from ai.scene_seed_s19 import SCENES
from ai.story_scene_seed import SCENES as STORY_SCENES
from ai.video_seed import VIDEO_CATALOG


class Command(BaseCommand):
    help = "Seed the Studio's creative controls (scenes, lights, camera angles, …)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Overwrite existing rows with the seed (discards admin edits).",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        created = updated = 0
        catalog = {**CATALOG, **VIDEO_CATALOG, **AD_TEMPLATE_CATALOG}
        # S19-12: the Pro scenes, shown only with `pro_rendering_s19` on.
        catalog["ad_template"] = [*catalog["ad_template"], *SCENES]
        # S20-01: the storyboard's ready-made scenes, served with `studio_storyboard_s20` on.
        catalog["story_scene"] = STORY_SCENES
        for kind, rows in catalog.items():
            for position, spec in enumerate(rows):
                defaults = {**spec, "sort_order": position * 10, "is_active": True}
                key = defaults.pop("key")
                defaults["translations"] = defaults.get("translations") or translations_for(
                    kind, key
                )
                if options["refresh"]:
                    _, was_created = CreativeOption.objects.update_or_create(
                        kind=kind, key=key, defaults=defaults
                    )
                    created, updated = created + was_created, updated + (not was_created)
                else:
                    _, was_created = CreativeOption.objects.get_or_create(
                        kind=kind, key=key, defaults=defaults
                    )
                    created += was_created
        self.stdout.write(
            self.style.SUCCESS(f"Creative options: {created} created, {updated} refreshed.")
        )
