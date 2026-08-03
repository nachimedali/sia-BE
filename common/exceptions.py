"""The single error envelope (design.md §7.1, A2, A3).

Every non-2xx response — DRF's own, Django's, and every domain exception —
leaves the API in exactly one shape:

    {"error": {"code": ..., "message": ..., "detail": {...}, "upgrade": {...}}}

`upgrade` appears only on 402, and 402 is used for nothing but entitlement
failures. That is what lets the frontend treat a 402 as "render an upgrade
prompt" without inspecting anything else (design.md §10.5).
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, HttpRequest
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

DEFAULT_UPGRADE_CTA = "/app/billing"


class OCCSError(drf_exceptions.APIException):
    """Base for domain exceptions that render into the envelope."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    default_code = "error"
    default_detail = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        self.message = message or str(self.default_detail)
        self.payload: dict[str, Any] = detail or {}
        self.code = code or self.default_code
        super().__init__(self.message, self.code)


# -----------------------------------------------------------------------------
# 402 — entitlements only (design.md A2)
# -----------------------------------------------------------------------------
class PaymentRequired(OCCSError):
    """Never raise this for anything but an entitlement failure.

    `upgrade` is built in the constructor rather than left to the caller, so a
    402 without an upgrade payload is unrepresentable.
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_code = "payment_required"
    default_detail = "Your plan does not include this."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
        suggested_plan: str = "pro",
        cta: str = DEFAULT_UPGRADE_CTA,
    ) -> None:
        super().__init__(message, detail=detail, code=code)
        self.upgrade: dict[str, Any] = {"suggested_plan": suggested_plan, "cta": cta}


class FeatureNotAvailable(PaymentRequired):
    default_code = "feature_not_available"
    default_detail = "This feature is not available on your plan."


class InsufficientCredits(PaymentRequired):
    default_code = "insufficient_credits"
    default_detail = "You do not have enough credits for this generation."


class InsufficientVideoUnits(PaymentRequired):
    default_code = "insufficient_video_units"
    default_detail = "You do not have enough video allowance for this generation."


class QuotaExceeded(PaymentRequired):
    default_code = "quota_exceeded"
    default_detail = "You have reached your plan's limit for this resource."


# -----------------------------------------------------------------------------
# Other domain failures
# -----------------------------------------------------------------------------
class StateConflict(OCCSError):
    """An illegal state-machine transition (design.md §7.1: 409)."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "state_conflict"
    default_detail = "The resource is not in a state that allows this transition."


class ProviderError(OCCSError):
    """An external provider failed or returned something unusable (422)."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "provider_error"
    default_detail = "An upstream provider could not complete this request."


class RateLimited(OCCSError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = "rate_limited"
    default_detail = "Too many requests."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
        retry_after: int | float = 1,
    ) -> None:
        super().__init__(message, detail=detail, code=code)
        self.retry_after = max(1, round(retry_after))


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------
_STATUS_CODES = {
    status.HTTP_400_BAD_REQUEST: "validation_error",
    status.HTTP_401_UNAUTHORIZED: "not_authenticated",
    status.HTTP_403_FORBIDDEN: "permission_denied",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_406_NOT_ACCEPTABLE: "not_acceptable",
    status.HTTP_409_CONFLICT: "state_conflict",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "provider_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "server_error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}


def build_envelope(
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    upgrade: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if detail:
        error["detail"] = detail
    if upgrade:
        error["upgrade"] = upgrade
    return {"error": error}


def _flatten_message(detail: Any) -> str:
    """Reduce DRF's nested detail structure to one human-readable sentence."""
    if isinstance(detail, list):
        return _flatten_message(detail[0]) if detail else ""
    if isinstance(detail, dict):
        for value in detail.values():
            message = _flatten_message(value)
            if message:
                return message
        return ""
    return str(detail)


def _normalise(exc: Exception) -> Exception:
    """Map Django's non-DRF exceptions onto their DRF equivalents."""
    if isinstance(exc, Http404):
        return drf_exceptions.NotFound()
    if isinstance(exc, DjangoPermissionDenied):
        return drf_exceptions.PermissionDenied()
    if isinstance(exc, DjangoValidationError):
        return drf_exceptions.ValidationError(
            getattr(exc, "message_dict", None) or list(exc.messages)
        )
    return exc


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    exc = _normalise(exc)
    response = drf_exception_handler(exc, context)

    if response is None:
        # An unhandled exception. Django's handler turns this into a 500; let it,
        # so the traceback still reaches Sentry rather than being swallowed here.
        return None

    if isinstance(exc, OCCSError):
        payload = build_envelope(
            code=exc.code,
            message=exc.message,
            detail=exc.payload,
            upgrade=getattr(exc, "upgrade", None),
        )
        if isinstance(exc, RateLimited):
            response["Retry-After"] = str(exc.retry_after)
    else:
        # drf_exception_handler only returns a response for APIException (and the
        # Django exceptions _normalise has already converted), so this is safe.
        api_exc = exc if isinstance(exc, drf_exceptions.APIException) else None
        exc_detail: Any = api_exc.detail if api_exc is not None else str(exc)

        detail: dict[str, Any] | None = None
        if isinstance(exc, drf_exceptions.ValidationError):
            # Field errors are the useful part of a 400; keep them structured.
            if isinstance(exc_detail, dict):
                detail = {"fields": exc_detail}
            elif isinstance(exc_detail, list):
                detail = {"errors": exc_detail}

        code = getattr(exc, "default_code", None) or _STATUS_CODES.get(
            response.status_code, "error"
        )
        # DRF uses "invalid" for every ValidationError; the envelope's code is
        # meant to be actionable, so prefer the status-derived name.
        if code in {"invalid", "error"}:
            code = _STATUS_CODES.get(response.status_code, "error")

        message = _flatten_message(exc_detail) or str(exc)
        payload = build_envelope(code=code, message=message, detail=detail)

        wait = getattr(exc, "wait", None)
        if isinstance(exc, drf_exceptions.Throttled) and wait is not None:
            response["Retry-After"] = str(round(wait))

    if (
        response.status_code == status.HTTP_402_PAYMENT_REQUIRED
        and "upgrade" not in (payload["error"])
    ):
        # design.md A2. Reaching here means a 402 was raised outside the
        # PaymentRequired family, which is a bug — but the contract still holds.
        logger.warning(
            "402 raised without an upgrade payload", extra={"code": payload["error"]["code"]}
        )
        payload["error"]["upgrade"] = {"suggested_plan": "pro", "cta": DEFAULT_UPGRADE_CTA}

    response.data = payload
    return response


# -----------------------------------------------------------------------------
# Django-level handlers
#
# DRF's handler only covers exceptions raised inside a DRF view. An unmatched
# URL or a crash in middleware is handled by Django and would otherwise return
# HTML, breaking the "one shape for every non-2xx" contract (A3) for exactly the
# responses a client is least prepared to parse.
# -----------------------------------------------------------------------------
def _wants_envelope(request: HttpRequest) -> bool:
    return bool(request.path.startswith("/api/"))


def api_404(request: Any, exception: Exception | None = None) -> Any:
    from django.http import JsonResponse
    from django.views.defaults import page_not_found

    if not _wants_envelope(request):
        return page_not_found(request, exception)
    return JsonResponse(build_envelope("not_found", "This endpoint does not exist."), status=404)


def api_500(request: Any) -> Any:
    from django.http import JsonResponse
    from django.views.defaults import server_error

    if not _wants_envelope(request):
        return server_error(request)
    return JsonResponse(build_envelope("server_error", "An unexpected error occurred."), status=500)
