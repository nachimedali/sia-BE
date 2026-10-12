"""Test settings.

Deliberately still backed by real Postgres and real Redis. The rate limiter is
a Lua script and the health check is a connectivity probe — neither means
anything against an in-memory fake, and both are the sort of thing that breaks
in production precisely because it was faked in CI.
"""

import tempfile
from pathlib import Path

from .base import *  # noqa: F403
from .base import REDIS_URL

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# Speed: the default hasher dominates runtime in auth-heavy suites.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Isolate test keyspace from the dev keyspace on the same Redis instance.
_TEST_REDIS_DB = 15
_test_redis_url = REDIS_URL.rsplit("/", 1)[0] + f"/{_TEST_REDIS_DB}"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _test_redis_url,
    }
}
RATELIMIT_REDIS_URL = _test_redis_url

# Mail, Stripe and media storage go to in-process fakes (design.md §9, A8) so no
# test depends on an external service being reachable or on test-mode keys
# existing. A temp dir per session keeps upload tests off the working tree.
USE_FAKE_MAIL = True
USE_FAKE_BILLING = True
USE_FAKE_STORAGE = True
USE_FAKE_PLATFORM_ADAPTER = True
USE_FAKE_TREND_VENDORS = True
USE_FAKE_PLAYBOOK_COLLECTORS = True
USE_FAKE_REFERENCE_IMAGES = True
PLAYBOOK_SHOW_SAMPLE = True
USE_FAKE_MEDIA_EDITOR = True
# The root conftest turns the master `USE_FAKE_AI_PROVIDERS` on for every
# test. The per-port switches are pinned off so the master alone decides: a
# test that turns it off reaches the real adapter (against a mocked transport),
# and a key in the local `.env` never changes what the suite does.
USE_FAKE_TEXT_PROVIDER = False
USE_FAKE_IMAGE_PROVIDER = False
USE_FAKE_VIDEO_PROVIDER = False
USE_FAKE_CHECKS_PROVIDER = False
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="occs-test-media-"))
STORAGES = {
    **STORAGES,  # noqa: F405
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Hosts the throwaway models the encryption tests need (see common/tests/apps.py).
INSTALLED_APPS = [*INSTALLED_APPS, "common.tests"]  # noqa: F405

# The suite builds its own catalogue rows; migration-seeded ones would collide.
# `common/tests/test_seed_migrations.py` turns it back on to test the seeds.
SEED_CATALOG_ON_MIGRATE = False
