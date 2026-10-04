"""Guard shared by the `*_seed_*` data migrations."""

from django.conf import settings


def seeding_enabled() -> bool:
    return bool(getattr(settings, "SEED_CATALOG_ON_MIGRATE", True))
