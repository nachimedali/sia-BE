"""`0037_template_capture_and_words`: existing template rows take the S17-26 seed.

A migration that has run never runs again, so a database seeded before this
still holds the old first-frame finish, the unused negative and the hooks that
put an invented experience in an invented person's mouth. Each field moves to
the new seed **only where it still holds the old seed's value**: an operator's
edit is never overwritten.
"""

from __future__ import annotations

import copy
import importlib
from typing import Any

import pytest
from django.apps import apps

from ai.ad_template_seed import CAPTURES, TEMPLATES
from ai.models import CreativeOption

MIGRATION = importlib.import_module("ai.migrations.0037_template_capture_and_words")
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seeding_on(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True


def _spec(key: str) -> dict[str, Any]:
    return next(spec for spec in TEMPLATES if spec["key"] == key)


def _old_row(key: str) -> CreativeOption:
    """The row as `0026`/`0029` seeded it: the old finish, the old negative,
    no capture, and the old words."""
    spec = copy.deepcopy(_spec(key))
    meta = spec["metadata"]
    capture = meta.pop("capture")
    meta["keyframe_prompt"] = (
        meta["keyframe_prompt"].removesuffix(CAPTURES[capture]["finish"]) + MIGRATION.OLD_FINISH
    )
    meta["negative"] = MIGRATION.OLD_NEGATIVE
    for field, value in MIGRATION.OLD_WORDS.get(key, {}).items():
        if field == "hooks":
            meta["hooks"] = list(value)
        else:
            meta["script"] = {**meta["script"], field: value}
    return CreativeOption.objects.create(
        kind="ad_template",
        key=key,
        label=spec["label"],
        metadata=meta,
    )


def test_an_untouched_row_takes_the_new_capture_negative_and_words() -> None:
    row = _old_row("reaction-demo")

    MIGRATION.forwards(apps, None)

    row.refresh_from_db()
    new = _spec("reaction-demo")["metadata"]
    assert row.metadata["capture"] == new["capture"]
    assert row.metadata["keyframe_prompt"] == new["keyframe_prompt"]
    assert row.metadata["negative"] == new["negative"]
    assert row.metadata["hooks"] == new["hooks"]
    assert row.metadata["script"] == new["script"]


def test_a_studio_template_loses_the_phone_finish() -> None:
    row = _old_row("levitate")

    MIGRATION.forwards(apps, None)

    row.refresh_from_db()
    assert "unpolished" not in row.metadata["keyframe_prompt"].lower()
    assert row.metadata["capture"] == "studio_hero"


def test_an_operators_edits_stay_field_by_field() -> None:
    row = _old_row("reaction-demo")
    meta = row.metadata
    meta["keyframe_prompt"] = "My own first frame."
    meta["negative"] = "no cats"
    meta["script"] = {**meta["script"], "hook": "Our own hook."}
    row.metadata = meta
    row.save(update_fields=["metadata"])

    MIGRATION.forwards(apps, None)

    row.refresh_from_db()
    new = _spec("reaction-demo")["metadata"]
    assert row.metadata["keyframe_prompt"] == "My own first frame."
    assert row.metadata["negative"] == "no cats"
    assert row.metadata["script"]["hook"] == "Our own hook."
    assert row.metadata["script"]["body"] == new["script"]["body"]  # untouched part moves
    assert row.metadata["capture"] == new["capture"]


def test_an_operators_own_template_is_left_alone() -> None:
    own = CreativeOption.objects.create(
        kind="ad_template",
        key="our-own",
        label="Ours",
        metadata={"keyframe_prompt": "x" + MIGRATION.OLD_FINISH, "negative": "y"},
    )
    MIGRATION.forwards(apps, None)
    own.refresh_from_db()
    assert "capture" not in own.metadata and own.metadata["negative"] == "y"


def test_running_twice_changes_nothing_more() -> None:
    row = _old_row("first-look")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    once = copy.deepcopy(row.metadata)
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert row.metadata == once


def test_off_under_test_settings(settings: Any) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = False
    row = _old_row("first-look")
    MIGRATION.forwards(apps, None)
    row.refresh_from_db()
    assert "capture" not in row.metadata


def test_every_old_word_is_still_what_the_previous_seed_held() -> None:
    """The old words are the migration's own record; each names a field the
    new seed changed, so nothing is moved that was never different."""
    seeded = {spec["key"] for spec in TEMPLATES}
    for key, fields in MIGRATION.OLD_WORDS.items():
        if key not in seeded:  # retired since (S17-32)
            continue
        meta = _spec(key)["metadata"]
        for field, old in fields.items():
            new = meta["hooks"] if field == "hooks" else meta["script"][field]
            assert new != (list(old) if field == "hooks" else old), (key, field)
