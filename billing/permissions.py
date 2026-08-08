"""Entitlement permission classes (design.md §8.1, I5).

I5 requires four independent gates: serializer, DRF permission, Celery task
preflight, and the UI. This is the second. Each has to block on its own — the
test for it bypasses the other three precisely so that "the serializer would
have caught it" is not an acceptable answer.

Raising `FeatureNotAvailable` rather than returning `False` is deliberate: DRF
turns a `False` into a 403, and an entitlement failure is a 402 carrying an
upgrade payload (design.md A2). A 403 would make the frontend render an error
toast where it should render an upgrade prompt.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from billing.services.entitlements import entitlements_for
from common.workspaces import active_workspace


def HasFeature(feature: str) -> type[BasePermission]:  # noqa: N802 — reads as a class
    """`permission_classes = [IsAuthenticated, HasFeature("playbook")]`.

    A factory returning a class, because DRF instantiates whatever is in
    `permission_classes` — so anything else has to fake being a class, and the
    tricks that achieve that are worse than one closure.
    """

    class _HasFeature(BasePermission):
        def has_permission(self, request: Request, view: Any) -> bool:
            if not request.user or not request.user.is_authenticated:
                return False
            entitlements_for(active_workspace(request)).require_feature(feature)
            return True

    _HasFeature.__name__ = f"HasFeature({feature!r})"
    return _HasFeature
