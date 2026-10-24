"""Captcha on the forms anyone can post to — a port, its real adapter and a fake.

Register, login, the password-reset request and the public token pages' actions
(`/r`, `/a`, `/g`) take a `captcha` field: the token the Cloudflare Turnstile
widget gave the browser. The view calls `require(request)` first thing, after
the throttle, so a refused request never reaches the work.

**The fake passes everything but the token `fail`**, so a fresh checkout, the
suite and the e2e run need no Cloudflare account (Part 7 rule 6), and a test can
still reach the refusal. It is on while `TURNSTILE_SECRET_KEY` is empty; the
deploy check warns about that (`common.W012`) rather than refusing, because the
key is an operator step.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from django.conf import settings
from rest_framework.request import Request

from common.exceptions import OCCSError

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT_SECONDS = 5.0
FIELD = "captcha"


class CaptchaFailedError(OCCSError):
    default_code = "captcha_failed"
    default_detail = "We could not check that you are human. Try again."


class CaptchaVerifier(Protocol):
    def verify(self, token: str) -> bool: ...


class TurnstileVerifier:
    def verify(self, token: str) -> bool:
        import httpx

        if not token:
            return False
        try:
            response = httpx.post(
                SITEVERIFY_URL,
                data={"secret": settings.TURNSTILE_SECRET_KEY, "response": token},
                timeout=TIMEOUT_SECONDS,
            )
            body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError):
            # Cloudflare unreachable: refuse. The form can be retried; an open
            # door during an outage is exactly when a bot would find it.
            logger.warning("turnstile siteverify unreachable", exc_info=True)
            return False
        return bool(body.get("success"))


class FakeCaptchaVerifier:
    def verify(self, token: str) -> bool:
        return token != "fail"


_override: CaptchaVerifier | None = None


def set_override(verifier: CaptchaVerifier | None) -> None:
    global _override
    _override = verifier


def get_captcha_verifier() -> CaptchaVerifier:
    if _override is not None:
        return _override
    if getattr(settings, "USE_FAKE_CAPTCHA", True):
        return FakeCaptchaVerifier()
    return TurnstileVerifier()


def require(request: Request) -> None:
    """`CaptchaFailedError` (400) unless the request carries a token that passes."""
    token = request.data.get(FIELD) if hasattr(request.data, "get") else None
    if not get_captcha_verifier().verify(str(token or "")):
        raise CaptchaFailedError()
