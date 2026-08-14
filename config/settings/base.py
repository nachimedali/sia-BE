"""Shared settings.

Everything environment-specific comes from the environment via django-environ
(design.md §11). No secret, host or credential is ever a literal here.
"""

from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
)

# Read .env when present; in production the environment is populated directly.
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# -----------------------------------------------------------------------------
# Applications
# -----------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_celery_beat",
    "corsheaders",
]

# design.md §5.2. Apps are added as their phase lands.
LOCAL_APPS = [
    "common",
    "accounts",
    "workspaces",
    "categories",
    "billing",
    "onboarding",
    "content",
    "products",
    "ai",
    "reminders",
    "scheduling",
    "channels",
    "trends",
    "analytics",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "common.middleware.RequestContextMiddleware",
    "common.middleware.ApiErrorEnvelopeMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# -----------------------------------------------------------------------------
# Data
# -----------------------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)

REDIS_URL = env("REDIS_URL")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# design.md §6.1. Declared in Phase 1 rather than Phase 2 on purpose: swapping
# AUTH_USER_MODEL after the first migration is a destructive change.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# -----------------------------------------------------------------------------
# i18n / time — implementation.md §4.7: store UTC, convert at the edges.
# -----------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# -----------------------------------------------------------------------------
# Media storage — design.md §11, implementation.md Phase 4. MinIO in dev, S3 in
# prod, addressed through django-storages either way (implementation.md §1.2).
#
# Falls back to the local filesystem whenever no endpoint is configured, the
# same shape as USE_FAKE_BILLING: CI and a fresh checkout have no MinIO to talk
# to, and media storage is not a business rule worth faking behind a port
# (design.md §9) — swapping the Django storage backend already is the fake.
# -----------------------------------------------------------------------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="occs-media")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default="")
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
AWS_S3_ADDRESSING_STYLE = "path"  # MinIO serves buckets path-style, not vhost-style.
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
AWS_QUERYSTRING_AUTH = True  # Media is workspace-private; URLs are signed, not public.

USE_FAKE_STORAGE = env.bool("USE_FAKE_STORAGE", default=not AWS_S3_ENDPOINT_URL)

STORAGES = {
    "default": {
        "BACKEND": (
            "django.core.files.storage.FileSystemStorage"
            if USE_FAKE_STORAGE
            else "storages.backends.s3.S3Storage"
        )
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# -----------------------------------------------------------------------------
# DRF — design.md §7
# -----------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "common.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # One envelope for every non-2xx response (design.md §7.1, A3).
    "EXCEPTION_HANDLER": "common.exceptions.exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=14)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "OCCS API",
    "DESCRIPTION": (
        "Marketing content system for product-led businesses. "
        "Every non-2xx response uses the single error envelope described in design.md §7.1; "
        "402 is reserved exclusively for entitlement failures and always carries `upgrade`."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        # `MediaAsset.kind` is serialised by two different serializers
        # (top-level and the nested per-post view); spectacular otherwise
        # can't tell the two `kind` enums apart and mangles the name.
        "MediaKindEnum": "content.models.MediaKind",
        # Same collision, `GenerationKind` on both `Generation.kind` and
        # `GenerationVariant.kind`.
        "GenerationKindEnum": "ai.models.GenerationKind",
        # And again for `Platform`, on `SocialAccount.platform` and the
        # connect-completion request's own `platform` field (Phase 9).
        "PlatformEnum": "content.models.Platform",
    },
}

# -----------------------------------------------------------------------------
# Celery — design.md §5.1
# -----------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_RESULT_EXTENDED = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Periodic billing bookkeeping. Declared here rather than only in the database
# scheduler so a fresh environment gets the schedule without a manual step;
# django_celery_beat syncs these into editable rows on first run.
CELERY_BEAT_SCHEDULE = {
    # Daily rather than monthly: each workspace's period is anchored on its own
    # signup date, so grants are due on every day of the month, not just the 1st.
    "billing-grant-monthly-allowances": {
        "task": "billing.tasks.grant_monthly_allowances",
        "schedule": crontab(hour=2, minute=15),
    },
    "billing-expire-trials": {
        "task": "billing.tasks.expire_trials",
        "schedule": crontab(hour=2, minute=45),
    },
    # Runs after the two writers above, so drift they introduce is caught the
    # same night rather than a day later.
    "billing-reconcile-ledgers": {
        "task": "billing.tasks.reconcile_ledgers",
        "schedule": crontab(hour=3, minute=30),
    },
    # Every minute, not daily like the billing jobs above: a reminder armed
    # for now+2m has to fire within that window, not by end of day
    # (implementation.md Phase 8's own done-when gate).
    "reminders-send-due": {
        "task": "reminders.tasks.send_due_reminders",
        "schedule": crontab(minute="*"),
    },
    "reminders-expire-stale": {
        "task": "reminders.tasks.expire_stale_reminders",
        "schedule": crontab(hour=4, minute=0),
    },
    # Every minute, for the same reason the reminder scan is: "a post
    # scheduled for 09:00 goes at 09:00" (design.md §5.1) is not a promise a
    # coarser cadence can keep.
    "scheduling-publish-due": {
        "task": "scheduling.tasks.publish_due_posts",
        "schedule": crontab(minute="*"),
    },
    # Hourly, because the metric ladder's tightest rung is T+1h (design.md
    # §8.9). The scan asks which targets are *owed* a capture rather than each
    # publish queueing five future tasks, so a missed hour backfills on the
    # next tick instead of being lost.
    "analytics-capture-due-metrics": {
        "task": "analytics.tasks.capture_due_metrics",
        "schedule": crontab(minute=5),
    },
    # Daily: follower counts move slowly, and this is the denominator
    # `engagement_rate` falls back to where a platform reports no impressions.
    "analytics-snapshot-accounts": {
        "task": "analytics.tasks.snapshot_accounts",
        "schedule": crontab(hour=1, minute=30),
    },
    # Nightly, as §8.9 specifies. After the capture and snapshot jobs, so it
    # scores against the freshest numbers rather than yesterday's.
    "analytics-scan-repurpose": {
        "task": "analytics.tasks.scan_repurpose_candidates",
        "schedule": crontab(hour=4, minute=30),
    },
    # Daily, though each config only runs on its own `cadence_days` — the scan
    # asks which configs are owed a run, so the cadence lives on the config
    # where an operator can retune it, not in this schedule. After the trend
    # and repurpose jobs, so a drafting run is grounded in the freshest corpus.
    "products-run-due-autopilot": {
        "task": "products.tasks.run_due_autopilot",
        "schedule": crontab(hour=5, minute=0),
    },
}

# -----------------------------------------------------------------------------
# Stripe — design.md §9, D13. The webhook is the source of truth for
# subscription state, so the signing secret is not optional in production.
# -----------------------------------------------------------------------------
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")
USE_FAKE_BILLING = env.bool("USE_FAKE_BILLING", default=not STRIPE_SECRET_KEY)

# Public URL of the frontend. Email links point here, not at the API.
SITE_URL = env("SITE_URL", default="http://localhost:3000")

# -----------------------------------------------------------------------------
# AI providers — design.md §9, D8/D9. Text targets a generic OpenAI-Chat-
# Completions-compatible endpoint (design.md names "LLM provider" without
# pinning a vendor); image targets Nano Banana 2 / Gemini 3.1 Flash Image
# (D8). Same USE_FAKE_* shape as billing/storage: no keys configured means no
# provider to fake around.
# -----------------------------------------------------------------------------
LLM_PROVIDER = env("LLM_PROVIDER", default="openai")
LLM_BASE_URL = env("LLM_BASE_URL", default="https://api.openai.com/v1")
LLM_API_KEY = env("LLM_API_KEY", default="")
LLM_DEFAULT_MODEL = env("LLM_DEFAULT_MODEL", default="gpt-4o-mini")
LLM_TIMEOUT_SECONDS = env.int("LLM_TIMEOUT_SECONDS", default=30)

IMAGE_PROVIDER = env("IMAGE_PROVIDER", default="nanobanana")
IMAGE_PROVIDER_BASE_URL = env(
    "IMAGE_PROVIDER_BASE_URL", default="https://generativelanguage.googleapis.com/v1beta"
)
IMAGE_PROVIDER_API_KEY = env("IMAGE_PROVIDER_API_KEY", default="")
IMAGE_PROVIDER_MODEL = env("IMAGE_PROVIDER_MODEL", default="gemini-3.1-flash-image")

USE_FAKE_AI_PROVIDERS = env.bool(
    "USE_FAKE_AI_PROVIDERS", default=not (LLM_API_KEY and IMAGE_PROVIDER_API_KEY)
)

# Embeddings share the LLM gateway (`LLM_BASE_URL`/`LLM_API_KEY`) and the same
# fake switch — one vendor relationship, three endpoints. Changing the model
# changes the vector width, which is a schema fact: see
# `trends.models.EMBEDDING_DIMENSIONS`.
EMBEDDING_MODEL = env("EMBEDDING_MODEL", default="text-embedding-3-small")

# -----------------------------------------------------------------------------
# Trend vendors — design.md §9, D12. Ad Library and Creative Center through a
# data vendor (which is where the ToS exposure belongs); YouTube and Reddit
# direct. Same USE_FAKE_* shape as every other port.
# -----------------------------------------------------------------------------
TREND_VENDOR_BASE_URL = env("TREND_VENDOR_BASE_URL", default="https://trends.p.rapidapi.com")
TREND_VENDOR_API_KEY = env("TREND_VENDOR_API_KEY", default="")
TREND_VENDOR_TIMEOUT_SECONDS = env.int("TREND_VENDOR_TIMEOUT_SECONDS", default=20)
YOUTUBE_API_KEY = env("YOUTUBE_API_KEY", default="")
# Reddit asks for a descriptive agent and rate-limits generic ones harder.
REDDIT_USER_AGENT = env("REDDIT_USER_AGENT", default="occs-trends/1.0")

USE_FAKE_TREND_VENDORS = env.bool(
    "USE_FAKE_TREND_VENDORS", default=not (TREND_VENDOR_API_KEY and YOUTUBE_API_KEY)
)

# -----------------------------------------------------------------------------
# Publishing provider — design.md §9, D2/D3, verified by V1 (§14). All platform
# credentials live with the provider; OCCS stores none (§6.2). Same USE_FAKE_*
# shape as billing/storage/AI: no key configured means no provider to fake
# around.
# -----------------------------------------------------------------------------
ZERNIO_BASE_URL = env("ZERNIO_BASE_URL", default="https://zernio.com/api")
ZERNIO_API_KEY = env("ZERNIO_API_KEY", default="")
ZERNIO_TIMEOUT_SECONDS = env.int("ZERNIO_TIMEOUT_SECONDS", default=30)

USE_FAKE_PLATFORM_ADAPTER = env.bool("USE_FAKE_PLATFORM_ADAPTER", default=not ZERNIO_API_KEY)

# The publishing provider fetches media by URL, so a storage-relative path is
# unusable to it. S3/MinIO already yields absolute URLs; local disk does not.
PUBLIC_MEDIA_BASE_URL = env("PUBLIC_MEDIA_BASE_URL", default=SITE_URL)

# -----------------------------------------------------------------------------
# Encryption — design.md §11. Built now even though v1 stores no platform
# tokens (D2): retrofitting encryption after data exists is painful.
# -----------------------------------------------------------------------------
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY")

# -----------------------------------------------------------------------------
# Rate limiting — design.md §11. Buckets are per (scope, identity).
# -----------------------------------------------------------------------------
RATELIMIT_REDIS_URL = env("RATELIMIT_REDIS_URL", default=REDIS_URL)

# -----------------------------------------------------------------------------
# CORS. The browser talks to Next.js, which proxies to Django server-side
# (design.md A1), so this is not on the main path — it exists for local tooling
# and the Advanced-tier direct API access (§4.1).
# -----------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

# -----------------------------------------------------------------------------
# Logging — structured JSON with request context (design.md §11).
# -----------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "common.logging.JSONFormatter",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
    },
}
