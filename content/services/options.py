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

from content.models import MediaAsset
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
    #: A `MediaAsset` id. Shaped like an `int` and validated like a reference —
    #: see `_resolve_media_asset`.
    "media": int,
}


class OptionError(ValueError):
    """Carries the per-key errors, so a composer can mark the field that is
    wrong rather than showing one message for a form of eight inputs."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {message}" for key, message in errors.items()))
        self.errors = errors


def validate(platform: str, options: dict[str, Any], *, workspace: Any = None) -> dict[str, Any]:
    """Returns the cleaned options, or raises `OptionError`.

    Cleaned rather than merely checked: absent keys with a declared default are
    filled in here, so every consumer downstream reads one shape and none of
    them has to remember what the default was.

    `workspace` is required only by `media`-kind options, and its absence is a
    **refusal**, not a skip. A caller that cannot say which workspace is asking
    must not get a pass on the one check that needs to know — failing open here
    is how another tenant's asset ends up as a thumbnail.
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
        if (
            problem is None
            and option.kind == "media"
            and _resolve_media_asset(value, workspace) is None
        ):
            problem = _media_problem(option, workspace)
        if problem:
            errors[key] = problem
        else:
            cleaned[key] = value

    if errors:
        raise OptionError(errors)
    return cleaned


def _check(option: Option, value: Any) -> str | None:
    """The shape check: is this value the right Python type for `option.kind`.
    No query — `media` is checked here only as "is this an int", the same as
    any other integer field. Ownership of the id is a separate question,
    answered by `_resolve_media_asset`, because *that* needs the tenant and
    this does not."""
    expected = _CHECKS.get(option.kind)
    if expected is None:  # pragma: no cover — a typo in the declaration
        return f"Unknown option kind '{option.kind}'."

    # `bool` is a subclass of `int` in Python, so an unguarded isinstance would
    # accept `True` for an integer field and store it as 1.
    if option.kind in {"int", "media"} and isinstance(value, bool):
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


def _media_problem(option: Option, workspace: Any) -> str:
    """The message for a `media` option that `_resolve_media_asset` could not
    resolve — split from the resolution itself so `validate` can say *why*
    (no workspace vs. no such asset) without a second query to find out."""
    if workspace is None:
        return f"{option.label} cannot be validated without a workspace."
    return f"{option.label} is not a media asset in this workspace."


def _resolve_media_asset(value: Any, workspace: Any) -> MediaAsset | None:
    """The **one** query behind every `media` option, shared by `validate`
    (existence is the whole check) and `resolve` (existence *and* the URL it
    renders to) — a media reference used to be checked once in each, which
    meant `resolve` paid for two queries to answer one question.

    Workspace-scoped, never a bare `pk` lookup: this is the tenancy guard —
    an id that exists but belongs to another workspace must resolve to
    nothing, the same as an id that does not exist at all.
    """
    if workspace is None:
        return None
    return MediaAsset.objects.filter(pk=value, workspace=workspace).first()


def resolve(platform: str, stored: dict[str, Any], *, workspace: Any = None) -> dict[str, Any]:
    """The render-time counterpart to `validate`, and it **never raises**.

    `validate` guards the way in, where a bad value is the caller's mistake and
    a 400 is the right answer. This guards the way out, where a bad value is
    already in the database — a rule retuned, an option retired, a thumbnail
    deleted — and raising would mean a post that cannot be previewed and cannot
    be published because of a setting nobody can see.

    So an undeclared or unusable key is **dropped**, and every declared default
    is filled in. Dropping an option loses a setting; sending one the provider
    does not know loses the whole publish.

    A `media` option resolves from an id to a **URL** here, for the same reason
    `AdaptedMedia.url` does: a provider cannot fetch a row from our database.
    Storing the id and rendering the URL is what keeps the reference checkable
    on the way in and usable on the way out.
    """
    declared = options_for(platform)
    resolved: dict[str, Any] = {}

    for key, option in declared.items():
        if key in stored:
            value = stored[key]
            if _check(option, value) is None:
                if option.kind == "media":
                    asset = _resolve_media_asset(value, workspace)
                    if asset is not None and asset.file:
                        resolved[key] = str(asset.file.url)
                        continue
                else:
                    resolved[key] = value
                    continue
        if option.default is not None:
            resolved[key] = option.default
    return resolved
