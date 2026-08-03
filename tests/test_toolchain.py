"""Proves the Phase 0 backend toolchain is wired.

Django itself is configured in Phase 1; these assertions only cover that pytest,
pytest-django and the settings module load, so that later phases start from a
verified baseline rather than an assumed one.
"""

from django.conf import settings


def test_django_settings_load() -> None:
    assert settings.configured
    assert settings.INSTALLED_APPS


def test_pytest_django_provides_a_database(db: None) -> None:
    """The `db` fixture resolving means pytest-django can create the test DB."""
    from django.db import connection

    assert connection.vendor
