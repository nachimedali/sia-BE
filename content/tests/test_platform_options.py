"""`platform_options`, validated against a declaration rather than a branch
(P1-02, P1-05).

**This file is Phase 4's insurance policy.** P4-06 is a hard stop: if adding
X, Pinterest and Google Business Profile needs new code paths rather than new
rows in `rules.py`, the abstraction is wrong and adding conditionals hides it.
The way to find that out cheaply is to check now that the validator branches on
*declared kind* and never on platform name.
"""

from __future__ import annotations

import pathlib

import pytest

from content.models import Platform
from content.services import options
from content.services.rules import PLATFORM_RULES, options_for


# -----------------------------------------------------------------------------
# The declaration is data
# -----------------------------------------------------------------------------
def test_the_validator_never_branches_on_platform_name() -> None:
    """The property Phase 4's cost depends on. A platform name appearing in
    this module is the first conditional, and the first is the expensive one —
    every later platform then arrives asking for its own."""
    source = pathlib.Path(options.__file__ or "").read_text()

    for platform in Platform.values:
        assert f'"{platform}"' not in source, (
            f"{platform} is named in the validator. Options are declared in rules.py; "
            "branching here is the P4-06 hard stop arriving early."
        )


def test_every_declared_option_has_a_checkable_kind() -> None:
    """A typo in a `kind` would otherwise surface as a validation that quietly
    accepts anything."""
    for platform, rule in PLATFORM_RULES.items():
        for option in rule.options:
            assert option.kind in options._CHECKS, f"{platform}.{option.key}: {option.kind}"


def test_a_choice_option_declares_its_choices() -> None:
    for rule in PLATFORM_RULES.values():
        for option in rule.options:
            if option.kind == "choice":
                assert option.choices, f"{option.key} is a choice with nothing to choose"


def test_a_platform_with_nothing_to_configure_declares_nothing() -> None:
    """Empty is the honest default — a platform should not inherit a union of
    every other platform's options."""
    assert options_for(Platform.THREADS) == {}


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def test_a_valid_option_set_passes_and_is_returned_cleaned() -> None:
    cleaned = options.validate(Platform.LINKEDIN, {"first_comment": "Thanks for reading"})

    assert cleaned["first_comment"] == "Thanks for reading"
    # Declared defaults are filled in, so every consumer downstream reads one
    # shape and none has to remember what the default was.
    assert cleaned["visibility"] == "PUBLIC"


def test_an_unknown_key_is_rejected_rather_than_ignored() -> None:
    """An ignored key is a composer field that silently does nothing — which
    the user experiences as the feature being broken and the developer
    experiences as nothing at all."""
    with pytest.raises(options.OptionError) as caught:
        options.validate(Platform.LINKEDIN, {"not_a_real_option": 1})

    assert "not_a_real_option" in caught.value.errors


def test_a_required_option_is_enforced() -> None:
    with pytest.raises(options.OptionError) as caught:
        options.validate(Platform.YOUTUBE, {})

    assert "title" in caught.value.errors


def test_a_choice_outside_the_declared_set_is_rejected() -> None:
    with pytest.raises(options.OptionError) as caught:
        options.validate(Platform.LINKEDIN, {"visibility": "SECRET"})

    assert "visibility" in caught.value.errors


def test_a_boolean_is_not_accepted_for_an_integer() -> None:
    """`bool` is a subclass of `int` in Python, so an unguarded isinstance
    would store `True` as a minimum age of 1."""
    with pytest.raises(options.OptionError) as caught:
        options.validate(Platform.FACEBOOK, {"targeting_min_age": True})

    assert "targeting_min_age" in caught.value.errors


def test_a_too_long_string_is_rejected() -> None:
    with pytest.raises(options.OptionError) as caught:
        options.validate(Platform.YOUTUBE, {"title": "t" * 101})

    assert "title" in caught.value.errors


def test_a_list_of_non_strings_is_rejected() -> None:
    with pytest.raises(options.OptionError) as caught:
        options.validate(Platform.FACEBOOK, {"targeting_countries": ["FR", 33]})

    assert "targeting_countries" in caught.value.errors


def test_every_error_is_reported_at_once() -> None:
    """One message for a form of eight inputs makes the user fix them one
    round-trip at a time."""
    with pytest.raises(options.OptionError) as caught:
        options.validate(Platform.YOUTUBE, {"privacy": "nope", "nonsense": 1})

    assert set(caught.value.errors) == {"privacy", "nonsense", "title"}


def test_defaults_are_not_invented_for_options_with_none() -> None:
    """An option with no declared default stays absent rather than becoming
    `None` — a key present with a null value reads as "explicitly unset", which
    is a different statement."""
    cleaned = options.validate(Platform.YOUTUBE, {"title": "A video"})

    assert "thumbnail_media_id" not in cleaned


# -----------------------------------------------------------------------------
# The declaration is served, not mirrored (P1-11)
# -----------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_declarations_are_served_to_the_frontend(
    auth_client: object, workspace: object
) -> None:
    """A TypeScript copy of `rules.py` is a second declaration of the same
    facts, and two declarations drift. Serving them is what keeps adding a
    platform a one-table change."""
    body = auth_client.get("/api/v1/platform-rules/").json()  # type: ignore[attr-defined]

    served = {row["platform"]: row for row in body["platforms"]}
    assert set(served) == set(PLATFORM_RULES)

    for platform, rule in PLATFORM_RULES.items():
        row = served[platform]
        assert row["char_limit"] == rule.char_limit
        assert [option["key"] for option in row["options"]] == [o.key for o in rule.options]

        # Per-format since P4-05. The platform-level cap is gone, and a test
        # still asserting one would be asserting the loosest of the formats'
        # numbers — the one value that is never right for any of them.
        served_formats = {entry["format"]: entry for entry in row["formats"]}
        assert set(served_formats) == set(rule.formats)
        for name, spec in rule.formats.items():
            assert served_formats[name]["max_media"] == spec.max_media
            assert served_formats[name]["min_media"] == spec.min_media
            assert served_formats[name]["allowed_media_kinds"] == sorted(spec.allowed_media_kinds)
            # Served resolved, never null: the composer needs the number that
            # applies, not a fallback rule it would have to reimplement.
            assert served_formats[name]["char_limit"] == (spec.char_limit or rule.char_limit)


@pytest.mark.django_db
def test_a_served_choice_option_carries_its_choices(auth_client: object, workspace: object) -> None:
    """Without them the composer cannot render the select, and would fall back
    to a free-text field that 400s on anything but the right word."""
    body = auth_client.get("/api/v1/platform-rules/").json()  # type: ignore[attr-defined]
    linkedin = next(row for row in body["platforms"] if row["platform"] == Platform.LINKEDIN)
    visibility = next(o for o in linkedin["options"] if o["key"] == "visibility")

    assert visibility["choices"] == ["PUBLIC", "CONNECTIONS"]
    assert visibility["default"] == "PUBLIC"


def test_a_remote_choice_option_is_served_with_its_source(
    auth_client: object, workspace: object
) -> None:
    """Without `source` the composer cannot know which list to fetch, and a
    Pinterest board would render as a text box that rejects whatever is typed
    into it (P4-02)."""
    body = auth_client.get("/api/v1/platform-rules/").json()  # type: ignore[attr-defined]
    pinterest = next(row for row in body["platforms"] if row["platform"] == "pinterest")
    board = next(option for option in pinterest["options"] if option["key"] == "board_id")

    assert board["kind"] == "remote_choice"
    assert board["source"] == "pinterest_boards"


def test_every_other_kind_is_served_with_an_empty_source(
    auth_client: object, workspace: object
) -> None:
    # Always present, never null: a client that had to branch on absence would
    # be one refactor away from branching on platform instead.
    body = auth_client.get("/api/v1/platform-rules/").json()  # type: ignore[attr-defined]
    for row in body["platforms"]:
        for option in row["options"]:
            assert "source" in option
            if option["kind"] != "remote_choice":
                assert option["source"] == ""
