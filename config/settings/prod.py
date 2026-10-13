"""Production.

Anything that must not be guessable or defaulted is read without a fallback, so
a missing value fails at boot rather than silently degrading security.
"""

from .base import *  # noqa: F403
from .base import env

DEBUG = False

# HTTPS / cookie hardening.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"

# Backend, port and TLS come from base.py's env reads; the four below are read
# again without a fallback so a deploy missing them fails at boot instead of
# trying to relay through localhost.
EMAIL_HOST = env("EMAIL_HOST")
EMAIL_HOST_USER = env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL")

SENTRY_DSN = env("SENTRY_DSN", default="")

# --- fakes cannot run here (P0-37, Part 7 rule 17) ---------------------------
#
# `base.py` defaults `USE_FAKE_PLATFORM_ADAPTER` to "on when no API key is
# configured", which is exactly right for a fresh checkout and exactly wrong
# here: a production deploy that lost its key would silently start fabricating
# metrics rather than failing. Pinned off, and the key is read without a
# fallback so a missing one stops the boot instead.
USE_FAKE_PLATFORM_ADAPTER = False
USE_FAKE_AI_PROVIDERS = False
USE_FAKE_TEXT_PROVIDER = False
USE_FAKE_IMAGE_PROVIDER = False
USE_FAKE_VIDEO_PROVIDER = False
USE_FAKE_CHECKS_PROVIDER = False
USE_FAKE_MEDIA_EDITOR = False
USE_FAKE_TREND_VENDORS = False
USE_FAKE_PLAYBOOK_COLLECTORS = False
USE_FAKE_REFERENCE_IMAGES = False
# The fake collector's rows are fixtures. A deploy with no YouTube key still
# collects from the fake (that is the registry's rule), and this is what keeps
# those rows off a customer's wall.
PLAYBOOK_SHOW_SAMPLE = False
# The filesystem fake is served without auth (config/urls.py). A deploy that
# lost AWS_S3_ENDPOINT_URL must fail at upload, not serve media publicly.
USE_FAKE_STORAGE = False
STORAGES = {**STORAGES, "default": {"BACKEND": "storages.backends.s3.S3Storage"}}  # noqa: F405
# The selected social provider's key is read without a fallback: a deploy that
# lost it must fail at boot, not quietly start publishing nowhere.
if SOCIAL_PROVIDER == "zernio":  # noqa: F405
    ZERNIO_API_KEY = env("ZERNIO_API_KEY")
else:
    SOCIALAPI_API_KEY = env("SOCIALAPI_API_KEY")
