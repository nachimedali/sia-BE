"""Validating `platform_options` against the declared schema (P1-05).

**Table-driven, with no branch on platform name.** The whole of Phase 4's cost
is decided here: adding X, Pinterest and Google Business Profile should be new
rows in `rules.py` and nothing else. If a platform ever needs a validator this
module cannot express, that is P4-06's hard stop — repair the abstraction
rather than adding a conditional, because the conditional is the thing that
makes the fourth platform cost what the first did.

Unknown keys are rejected rather than ignored. An ignored key is a composer
field that silently does nothing, which the user experiences as the feature
being broken and the developer experiences as nothing at all.
"""

from __future__ import annotations

from typing import Any, cast

from content.services.rules import Option, options_for

#: The `kind` vocabulary, and how each one is checked. Deliberately tiny —
#: anything richer would be a schema language, and a schema language nobody can
#: validate is worse than a short list of types.
_CHECKS: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "url": str,
    "choice": str,
    "int": int,
    "bool": bool,
    "list[str]": list,
}


class OptionError(ValueError):
    """Carries the per-key errors, so a composer can mark the field that is
    wrong rather than showing one message for a form of eight inputs."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {message}" for key, message in errors.items()))
        self.errors = errors


def validate(platform: str, options: dict[str, Any]) -> dict[str, Any]:
    """Returns the cleaned options, or raises `OptionError`.

    Cleaned rather than merely checked: absent keys with a declared default are
    filled in here, so every consumer downstream reads one shape and none of
    them has to remember what the default was.
    """
    declared = options_for(platform)
    errors: dict[str, str] = {}

    unknown = set(options) - set(declared)
    for key in sorted(unknown):
        errors[key] = f"{platform} has no option '{key}'."

    cleaned: dict[str, Any] = {}
    for key, option in declared.items():
        if key not in options:
            if option.required:
                errors[key] = f"{option.label} is required for {platform}."
            elif option.default is not None:
                cleaned[key] = option.default
            continue

        value = options[key]
        problem = _check(option, value)
        if problem:
            errors[key] = problem
        else:
            cleaned[key] = value

    if errors:
        raise OptionError(errors)
    return cleaned


def _check(option: Option, value: Any) -> str | None:
    expected = _CHECKS.get(option.kind)
    if expected is None:  # pragma: no cover — a typo in the declaration
        return f"Unknown option kind '{option.kind}'."

    # `bool` is a subclass of `int` in Python, so an unguarded isinstance would
    # accept `True` for an integer field and store it as 1.
    if option.kind == "int" and isinstance(value, bool):
        return f"{option.label} must be a whole number."
    if not isinstance(value, expected):
        return f"{option.label} must be a {option.kind}."

    if option.kind == "choice" and value not in option.choices:
        return f"{option.label} must be one of: {', '.join(option.choices)}."
    if option.kind == "list[str]":
        # `isinstance(value, list)` above has already run, but mypy narrows on
        # the literal type rather than the table lookup, so the cast is what
        # tells it what the table already guaranteed.
        items = cast("list[Any]", value)
        if not all(isinstance(item, str) for item in items):
            return f"{option.label} must be a list of strings."
    if option.max_length is not None and isinstance(value, str) and len(value) > option.max_length:
        return f"{option.label} must be at most {option.max_length} characters."
    return None
