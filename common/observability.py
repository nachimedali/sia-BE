"""Error reporting to Sentry (F14).

Initialised from `CommonConfig.ready()`, after the settings module is final, so
the web, Celery worker and beat processes all report and the test settings'
blank DSN is the one read. No DSN, no SDK: a fresh checkout sends nothing.
The Django, Celery and Redis integrations enable themselves when installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sentry_sdk
from django.conf import settings

from common.logging import request_id_var, user_id_var, workspace_id_var

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint


def before_send(event: Event, hint: Hint) -> Event:
    """Tag the event with the ids the JSON logs carry, so one id finds both.
    The user is an id only: no email, no IP (`send_default_pii` stays off)."""
    ids = (("request_id", request_id_var.get()), ("workspace_id", workspace_id_var.get()))
    tags = {key: value for key, value in ids if value is not None}
    if tags:
        event["tags"] = {**(event.get("tags") or {}), **tags}
    if (user_id := user_id_var.get()) is not None:
        event["user"] = {"id": user_id}
    return event


def init_sentry() -> bool:
    if not settings.SENTRY_DSN:
        return False
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE or None,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        profile_session_sample_rate=settings.SENTRY_PROFILE_SESSION_SAMPLE_RATE,
        profile_lifecycle="trace",
        send_default_pii=False,
        enable_logs=True,
        before_send=before_send,
    )
    return True
