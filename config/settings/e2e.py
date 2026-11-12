"""Settings for the Playwright end-to-end run.

Development settings, with two changes that make the flow observable and
self-contained:

  * mail is written to files, so the suite can read the real verification link
    out of a real rendered email rather than reaching into the database for a
    token that the user would never see;
  * Celery runs eagerly, so no worker process is needed.
"""

from __future__ import annotations

import os

from .dev import *  # noqa: F403
from .dev import BASE_DIR, DATABASES, SIMPLE_JWT

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

# The frontend the emailed links must point at; Playwright passes its port
# (`E2E_PORT`), so the suite can run beside another app holding 3100.
SITE_URL = os.environ.get("E2E_SITE_URL", "http://127.0.0.1:3100")

# Playwright drives every worker from 127.0.0.1, so the per-IP auth limits would
# throttle the suite itself. The limiter is covered by its own backend tests
# (accounts/tests/test_throttling.py) under time-machine, which is the right
# place for it. The refill matters as much as the capacity: at the production
# rate (one sign-up every two minutes) one full run drains the 500 and the next
# run's late specs stay stuck on /register.
_E2E_THROTTLE = {"capacity": 500, "refill_per_second": 50}
AUTH_THROTTLES = {
    "auth:login": _E2E_THROTTLE,
    "auth:register": _E2E_THROTTLE,
    "auth:reset": _E2E_THROTTLE,
    "auth:resend": _E2E_THROTTLE,
}

# Playwright never reaches an AI vendor, whatever keys the local `.env` holds.
USE_FAKE_AI_PROVIDERS = True

# The Playbook runs on its fake collectors and a fake picture reader: the suite
# never reaches YouTube, Google or an image host.
USE_FAKE_PLAYBOOK_COLLECTORS = True
USE_FAKE_REFERENCE_IMAGES = True
PLAYBOOK_SHOW_SAMPLE = True
PLAYBOOK_SAMPLE_IMAGE_BASE = f"{SITE_URL}/playbook-sample/"

# Playwright never reaches a social provider, whatever keys the local `.env`
# holds: publishing and capture run on the fakes.
USE_FAKE_PLATFORM_ADAPTER = True

# Playwright cannot solve a real Turnstile check: the captcha runs on its fake,
# and the frontend server it boots draws no widget (`playwright.config.ts`).
USE_FAKE_CAPTCHA = True
SENTRY_DSN = ""  # Playwright failures are expected, not incidents

# Every "signed in" spec reuses the one session `e2e/auth.setup.ts` saved. A full
# run outlasts the 15-minute access cookie, and with rotation the first spec to
# refresh spends the shared refresh token and signs every later one out (they
# land on /login, answering 200 where a 404 or 500 was asserted). Rotation is
# covered by `accounts/tests/test_auth.py`; sign-out still blacklists explicitly.
SIMPLE_JWT = {**SIMPLE_JWT, "ROTATE_REFRESH_TOKENS": False, "BLACKLIST_AFTER_ROTATION": False}
