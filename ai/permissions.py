"""The DRF permission gate for generation credits (design.md §8.1, I5).

I5 wants four independent gates: serializer, DRF permission, Celery task
preflight, and the UI. `billing/permissions.py::HasFeature` is the shape for
a feature flag that gates a whole endpoint statically; `POST /ai/generate/`
is polymorphic on `kind`/`mode` in the request body, so the check here reads
the body rather than being a static flag — same principle (block here,
independent of whatever the serializer or the service also do), different
mechanics because the endpoint itself is shaped differently.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from ai.services.costing import preflight_require_credits
from common.workspaces import active_workspace


class HasSufficientCredits(BasePermission):
    def has_permission(self, request: Request, view: Any) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False

        data = request.data
        kind = data.get("kind") if isinstance(data, dict) else None
        if not kind:
            # Nothing to price yet — the serializer reports a missing kind.
            return True

        mode = data.get("mode", "") if isinstance(data, dict) else ""
        preflight_require_credits(active_workspace(request), kind=kind, mode=mode)
        return True
