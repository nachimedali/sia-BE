"""Structured JSON logging (design.md §11).

Every record carries workspace / user / request ids when the request-context
middleware has bound them, so a production incident can be traced across web
and worker processes by a single id.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from contextvars import ContextVar
from typing import Any

# Bound per request (and copied into Celery headers in later phases).
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
workspace_id_var: ContextVar[str | None] = ContextVar("workspace_id", default=None)

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in (
            ("request_id", request_id_var.get()),
            ("user_id", user_id_var.get()),
            ("workspace_id", workspace_id_var.get()),
        ):
            if value is not None:
                payload[key] = value

        # Anything passed via logger.info("...", extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)
