"""Local development."""

from .base import *  # noqa: F403
from .base import env

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]

INSTALLED_APPS = [*INSTALLED_APPS]  # noqa: F405

# Convenience only: never let a dev machine's browsability leak into prod.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

CORS_ALLOWED_ORIGINS = env(
    "CORS_ALLOWED_ORIGINS",
    default=["http://localhost:3000", "http://127.0.0.1:3000"],
)
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

# The Playbook's sample pictures live in the frontend's `public/` folder, and
# a dev machine without a YouTube key is exactly where samples are wanted.
# Blank in `.env` means the dev default (on), not "off".
_show_sample = env("PLAYBOOK_SHOW_SAMPLE", default="")
PLAYBOOK_SHOW_SAMPLE = env.bool("PLAYBOOK_SHOW_SAMPLE") if _show_sample else True
PLAYBOOK_SAMPLE_IMAGE_BASE = (
    env("PLAYBOOK_SAMPLE_IMAGE_BASE", default="") or "http://localhost:3000/playbook-sample/"
)
