"""Sends one test error and one log line to Sentry, to prove the DSN works.

    python manage.py sentry_check

A command rather than a public `/sentry-debug/` URL: a route that raises on
demand is a way for anyone to spend the Sentry quota.
"""

from __future__ import annotations

import sentry_sdk
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from sentry_sdk import logger as sentry_logger

from common.observability import init_sentry


class Command(BaseCommand):
    help = "Send a test error and a test log to Sentry."

    def handle(self, *args: object, **options: object) -> None:
        if not settings.SENTRY_DSN or not init_sentry():
            raise CommandError("SENTRY_DSN is not set; nothing to check.")
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError as exc:
            event_id = sentry_sdk.capture_exception(exc)
        sentry_logger.error("sentry_check: test log from manage.py")
        sentry_sdk.flush(timeout=10)
        self.stdout.write(f"Sent test error {event_id} and a test log to Sentry.")
