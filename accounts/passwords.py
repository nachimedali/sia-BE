"""Password strength, in one place: registration, reset, the account page's
password change and accepting an invite all ask the same validators."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError


def password_problems(password: str, *, user: Any = None) -> list[str]:
    """What is wrong with `password`, empty when nothing is. `user` lets the
    similarity validator compare it with the person's email and name."""
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        return list(exc.messages)
    return []
