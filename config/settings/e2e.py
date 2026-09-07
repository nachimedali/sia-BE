"""Settings for the Playwright end-to-end run.

Development settings, with two changes that make the flow observable and
self-contained:

  * mail is written to files, so the suite can read the real verification link
    out of a real rendered email rather than reaching into the database for a
    token that the user would never see;
  * Celery runs eagerly, so no worker process is needed.
"""

from __future__ import annotations

from .dev import *  # noqa: F403
from .dev import BASE_DIR, DATABASES

# **Its own database, not the dev one.** The suite shares a Postgres server
# with development, and pointing both at one database means a long-lived dev
# schema — including tables left behind by other build lines — decides whether
# the end-to-end run passes. A dedicated database makes the run reproducible
# from migrations alone, and lets it be dropped and rebuilt without touching
# anyone's development data.
DATABASES["default"] = {**DATABASES["default"], "NAME": "occs_e2e"}

EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = BASE_DIR / ".e2e-mail"
EMAIL_FILE_PATH.mkdir(exist_ok=True)

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# The frontend the emailed links must point at.
SITE_URL = "http://127.0.0.1:3100"

# Playwright drives every worker from 127.0.0.1, so the per-IP auth limits would
# throttle the suite itself. The limiter is covered by its own backend tests
# (accounts/tests/test_throttling.py) under time-machine, which is the right
# place for it.
AUTH_THROTTLES = {
    "auth:login": {"capacity": 500},
    "auth:register": {"capacity": 500},
    "auth:reset": {"capacity": 500},
    "auth:resend": {"capacity": 500},
}
