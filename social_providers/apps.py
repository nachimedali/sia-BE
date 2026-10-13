from django.apps import AppConfig


class SocialProvidersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "social_providers"
    verbose_name = "Social providers"

    def ready(self) -> None:
        # Imported for its `@register` side effect: Django's documented way to
        # add system checks. Not a signal (rule 7) — nothing runs on an event.
        from social_providers import checks  # noqa: F401
