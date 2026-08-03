from django.apps import AppConfig


class CommonTestsConfig(AppConfig):
    """Hosts throwaway models used only by the test suite.

    Registered from config.settings.test only. There is no migrations package,
    so the table is created by run_syncdb during test database setup.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "common.tests"
    label = "common_tests"
