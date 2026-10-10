"""Seeds the six tool rows (design.md §8.10).

Idempotent, like every other seed command: re-running updates in place rather
than duplicating, so it is safe on every deploy. Operators edit `credits_cost`
and `is_enabled` in admin afterwards — this only establishes the starting point.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from tools.models import Tool, ToolConfig

TOOLS: list[dict[str, Any]] = [
    {"slug": Tool.HEADLINE, "display_name": "Headline generator", "sort_order": 0},
    {"slug": Tool.HOOK, "display_name": "Hook writer", "sort_order": 1},
    {"slug": Tool.BIO, "display_name": "Bio writer", "sort_order": 2},
    # The three below cost a credit too, but spend it on our own corpus rather
    # than on a provider — the price is uniform because the user's decision is
    # "is this worth a credit", not "which of these is cheap for us".
    {"slug": Tool.HASHTAG, "display_name": "Hashtag ranker", "sort_order": 3},
    {"slug": Tool.THREAD_SPLITTER, "display_name": "Thread splitter", "sort_order": 4},
    {"slug": Tool.BEST_TIME, "display_name": "Best time to post", "sort_order": 5},
]


class Command(BaseCommand):
    help = "Seeds the six quick tools."

    def handle(self, *args: Any, **options: Any) -> None:
        for spec in TOOLS:
            tool, created = ToolConfig.objects.update_or_create(
                slug=spec["slug"], defaults={k: v for k, v in spec.items() if k != "slug"}
            )
            self.stdout.write(f"  {'created' if created else 'updated'}: {tool.slug}")
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(TOOLS)} tools."))
