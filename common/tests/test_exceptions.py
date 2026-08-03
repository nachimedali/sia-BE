"""The single error envelope (design.md §7.1, A2, A3)."""

from __future__ import annotations

import pytest
from rest_framework import exceptions as drf_exceptions
from rest_framework.test import APIRequestFactory

from common.exceptions import (
    FeatureNotAvailable,
    InsufficientCredits,
    PaymentRequired,
    ProviderError,
    QuotaExceeded,
    RateLimited,
    StateConflict,
    exception_handler,
)


def _handle(exc: Exception):
    request = APIRequestFactory().get("/api/v1/anything/")
    return exception_handler(exc, {"request": request, "view": None})


def _error(exc: Exception) -> dict:
    response = _handle(exc)
    assert response is not None
    assert set(response.data) == {"error"}, "the envelope has exactly one top-level key"
    return response.data["error"]


@pytest.mark.parametrize(
    ("exc", "expected_status", "expected_code"),
    [
        (
            drf_exceptions.ValidationError({"email": ["Enter a valid email."]}),
            400,
            "validation_error",
        ),
        (drf_exceptions.NotAuthenticated(), 401, "not_authenticated"),
        (drf_exceptions.PermissionDenied(), 403, "permission_denied"),
        (drf_exceptions.NotFound(), 404, "not_found"),
        (drf_exceptions.MethodNotAllowed("POST"), 405, "method_not_allowed"),
        (StateConflict(), 409, "state_conflict"),
        (ProviderError(), 422, "provider_error"),
        (RateLimited(), 429, "rate_limited"),
        (PaymentRequired(), 402, "payment_required"),
    ],
)
def test_error_envelope_shape(exc: Exception, expected_status: int, expected_code: str) -> None:
    response = _handle(exc)
    assert response is not None
    assert response.status_code == expected_status

    error = response.data["error"]
    assert error["code"] == expected_code
    assert isinstance(error["message"], str) and error["message"]
    # `detail` and `upgrade` are optional; nothing else may appear.
    assert set(error) <= {"code", "message", "detail", "upgrade"}


def test_validation_errors_keep_their_field_structure() -> None:
    error = _error(drf_exceptions.ValidationError({"email": ["Enter a valid email."]}))
    assert error["detail"]["fields"]["email"] == ["Enter a valid email."]
    assert error["message"] == "Enter a valid email."


@pytest.mark.parametrize(
    "exc",
    [
        PaymentRequired(),
        FeatureNotAvailable(),
        InsufficientCredits(),
        QuotaExceeded(),
    ],
)
def test_every_402_carries_an_upgrade_payload(exc: PaymentRequired) -> None:
    """design.md A2 — this is the frontend's signal to render an upgrade prompt
    rather than an error toast, so it can never be absent."""
    response = _handle(exc)
    assert response is not None
    assert response.status_code == 402

    upgrade = response.data["error"]["upgrade"]
    assert upgrade["suggested_plan"]
    assert upgrade["cta"] == "/app/billing"


def test_402_raised_outside_the_family_still_gets_an_upgrade() -> None:
    """A backstop, not a licence: 402 must never reach the client bare."""

    class RogueEntitlementError(drf_exceptions.APIException):
        status_code = 402
        default_detail = "nope"
        default_code = "rogue"

    error = _error(RogueEntitlementError())
    assert error["upgrade"] == {"suggested_plan": "pro", "cta": "/app/billing"}


def test_insufficient_credits_carries_actionable_detail() -> None:
    error = _error(
        InsufficientCredits(
            "This generation needs 3 credits; 1 remaining.",
            detail={"required": 3, "available": 1},
        )
    )
    assert error["code"] == "insufficient_credits"
    assert error["detail"] == {"required": 3, "available": 1}


def test_rate_limited_sets_retry_after_header() -> None:
    response = _handle(RateLimited(retry_after=42.4))
    assert response is not None
    assert response["Retry-After"] == "42"


def test_throttled_sets_retry_after_header() -> None:
    response = _handle(drf_exceptions.Throttled(wait=30))
    assert response is not None
    assert response.status_code == 429
    assert response["Retry-After"] == "30"


def test_django_exceptions_are_normalised_into_the_envelope() -> None:
    from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.http import Http404

    assert _handle(Http404()).status_code == 404
    assert _handle(DjangoPermissionDenied()).status_code == 403

    response = _handle(DjangoValidationError({"name": ["Too short."]}))
    assert response.status_code == 400
    assert response.data["error"]["detail"]["fields"]["name"] == ["Too short."]


def test_unhandled_exceptions_are_left_to_django() -> None:
    """Swallowing these here would hide the traceback from Sentry."""
    assert _handle(RuntimeError("boom")) is None
