from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "common"

    def ready(self) -> None:
        # Registers the deploy checks. Not a signal (rule 7).
        from common import checks  # noqa: F401
