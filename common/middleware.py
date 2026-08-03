"""Binds request context for structured logging (design.md §11)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from common.logging import request_id_var, user_id_var, workspace_id_var

REQUEST_ID_HEADER = "HTTP_X_REQUEST_ID"


class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Honour an upstream id so a trace survives the Next.js BFF hop.
        request_id = request.META.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.request_id = request_id  # type: ignore[attr-defined]

        tokens = [
            request_id_var.set(request_id),
            user_id_var.set(None),
            workspace_id_var.set(None),
        ]
        try:
            response = self.get_response(request)
        finally:
            for var, token in zip(
                (request_id_var, user_id_var, workspace_id_var), tokens, strict=True
            ):
                var.reset(token)

        response["X-Request-ID"] = request_id
        return response


class ApiErrorEnvelopeMiddleware:
    """Guarantees the envelope on API 404s in every environment.

    `handler404` is bypassed when DEBUG is on — Django serves its technical
    404 page instead — so without this, an unmatched /api/ path returns HTML in
    development and JSON in production. That divergence is worst exactly where
    it matters: a frontend developer debugging a typo'd endpoint gets an HTML
    parse error instead of the documented shape (design.md A3).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)

        if (
            response.status_code == 404
            and request.path.startswith("/api/")
            and not response.headers.get("Content-Type", "").startswith("application/json")
        ):
            from common.exceptions import build_envelope

            return JsonResponse(
                build_envelope("not_found", "This endpoint does not exist."), status=404
            )

        return response
