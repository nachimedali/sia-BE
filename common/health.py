"""Liveness/readiness probe (design.md §7.2, A7).

200 only when Postgres and Redis are both reachable. Provider reachability is
reported but excluded from the 200/503 decision on purpose: a flaky vendor must
not be able to take the app down or trigger a rollback.
"""

from __future__ import annotations

import time
from typing import Any

from django.db import connection
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import build_envelope
from common.redis import get_redis


def _timed(check: Any) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        check()
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc)[:200],
        }
    return {
        "status": "ok",
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def check_postgres() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def check_redis() -> None:
    get_redis().ping()


class HealthView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @extend_schema(
        operation_id="health",
        summary="Service health",
        description=(
            "200 when Postgres and Redis are both reachable, 503 otherwise. "
            "Provider reachability is reported but never affects the status code."
        ),
        responses={200: dict, 503: dict},
        auth=[],
    )
    def get(self, request: Request) -> Response:
        checks = {"postgres": _timed(check_postgres), "redis": _timed(check_redis)}

        # Reported, never blocking (A7). Populated as adapters land in later phases.
        providers: dict[str, Any] = {}

        healthy = all(check["status"] == "ok" for check in checks.values())
        body = {
            "status": "ok" if healthy else "degraded",
            "checks": checks,
            "providers": providers,
        }

        if healthy:
            return Response(body, status=status.HTTP_200_OK)

        failed = [name for name, check in checks.items() if check["status"] != "ok"]
        # Still the single envelope (design.md A3); the per-dependency report
        # travels in `detail` so probes and dashboards lose nothing.
        return Response(
            build_envelope(
                code="service_unavailable",
                message=f"Unhealthy dependencies: {', '.join(failed)}.",
                detail=body,
            ),
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class BeatHealthView(APIView):
    """503 when Celery Beat has not fired for `BEAT_STALE_SECONDS`, for an uptime
    check. Separate from `/health/`: a stopped Beat must not take the site out of rotation."""

    authentication_classes: list[Any] = []
    permission_classes: list[Any] = [AllowAny]

    @extend_schema(
        operation_id="health_beat",
        summary="Celery Beat heartbeat",
        responses={200: dict, 503: dict},
        auth=[],
    )
    def get(self, request: Request) -> Response:
        from django.conf import settings

        from common.tasks import BEAT_HEARTBEAT_KEY

        try:
            raw = get_redis().get(BEAT_HEARTBEAT_KEY)
        except Exception:
            raw = None
        age = None if raw is None else int(time.time()) - int(raw)
        limit = settings.BEAT_STALE_SECONDS
        if age is not None and age <= limit:
            return Response({"status": "ok", "age_seconds": age})
        return Response(
            build_envelope(
                code="beat_stale",
                message="Celery Beat has not fired recently.",
                detail={"age_seconds": age, "limit_seconds": limit},
            ),
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
