"""S17-38 — the talking templates: Happy customer, 3 reasons, Reply to a comment.

A person's own lines are spoken by the avatar with its lips moving: the prompt
quotes them in their beats, asks the model for sound and never says "no
speech" — while "no music" stays, because music is the composer's bed, mixed
under the voice and only when the person picks one. A missing or too-long line
is refused before anything is charged. Growth and Scale only (the plan's
`ad_templates_talking`).
"""

# ruff: noqa: F811 — pytest fixtures imported from test_ad_templates
from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.apps import apps

from ai.models import CreativeKind, CreativeOption, VideoModelProfile, VideoRender
from ai.providers.fake import _fake_video_composer, _fake_video_provider
from ai.services import prompt_lint, video_prompt
from ai.tests.test_ad_templates import (  # noqa: F401 — fixtures
    ESTIMATE,
    GALLERY,
    RENDERS,
    avatar_for,
    catalog,
    code_of,
    confirmed,
    flag_on,
    growth,
    lina,
    make_product,
    oil,
    on_plan,
)
from billing.services import ledger
from workspaces.services.provisioning import provision_workspace

pytestmark = pytest.mark.django_db

TALKING = ("founder-story", "three-reasons", "comment-reply")
LINES = {
    "founder-story": {
        "hook": "I almost skipped it.",
        "body": "Now I use it daily.",
        "cta": "Try it.",
    },
    "three-reasons": {
        "hook": "Three reasons.",
        "body": "Cold-pressed · Keeps for months · Tastes green",
        "cta": "See for yourself.",
    },
    "comment-reply": {
        "hook": "Is it worth it?",
        "body": "Yes, every single drop.",
        "cta": "Ask me more.",
    },
}


#: What each one says aloud: the reply shows the typed question as a bubble.
SPOKEN = {
    "founder-story": ["I almost skipped it.", "Now I use it daily.", "Try it."],
    "three-reasons": ["Three reasons.", "Cold-pressed", "Keeps for months", "Tastes green"],
    "comment-reply": ["Yes, every single drop.", "Ask me more."],
}


def talking(product: Any, avatar: Any, key: str, **over: Any) -> dict[str, Any]:
    return {
        "mode": "clip",
        "length": "clip-10",
        "template": key,
        "product": product.pk,
        "avatar": avatar.pk,
        "language": "en",
        "script": dict(LINES[key]),
        **over,
    }


def render(client: Any, body: dict[str, Any]) -> VideoRender:
    response = client.post(RENDERS, confirmed(client, body), format="json")
    assert response.status_code == 202, response.json()
    made = VideoRender.objects.get(pk=response.json()["id"])
    assert made.status == "SUCCEEDED", (made.error_code, made.error)
    return made


# -----------------------------------------------------------------------------
# On, at 10 s, Growth and Scale
# -----------------------------------------------------------------------------
def test_the_three_are_out_at_ten_seconds_with_their_lines(auth_client: Any, oil: Any) -> None:
    body = auth_client.get(GALLERY, {"product": oil.pk}).json()
    rows = {row["key"]: row for row in body["templates"]}
    for key in TALKING:
        assert rows[key]["available"] is True, key
        assert rows[key]["description"] != "", key
        assert [length["key"] for length in rows[key]["lengths"]] == ["clip-10"], key
        assert rows[key]["lock"] is None, key  # Growth has ad_templates_talking
        assert rows[key]["speech"], key
    assert rows["founder-story"]["label"] == "Happy customer"
    assert [line["part"] for line in rows["three-reasons"]["speech"]] == [
        "hook",
        "claim1",
        "claim2",
        "claim3",
    ]


def test_below_growth_the_talking_templates_are_locked(
    auth_client: Any, oil: Any, plans: dict[str, Any]
) -> None:
    on_plan(oil.workspace, plans, "essential")
    rows = {
        row["key"]: row for row in auth_client.get(GALLERY, {"product": oil.pk}).json()["templates"]
    }
    for key in TALKING:
        assert rows[key]["lock"]["feature"] == "ad_templates_talking", key


# -----------------------------------------------------------------------------
# The prompt speaks the person's lines
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("key", TALKING)
def test_the_avatar_says_the_persons_lines_with_sound(
    key: str, auth_client: Any, oil: Any, lina: Any
) -> None:
    _fake_video_provider.clear()
    made = render(auth_client, talking(oil, lina, key))

    prompt = made.prompt_sent
    for line in SPOKEN[key]:
        assert f'says: "{line}"' in prompt, (line, prompt)
    assert "no speech" not in prompt.lower()
    assert "no music" in prompt.lower()
    assert "lips" in prompt.lower()
    assert _fake_video_provider.requests[-1]["audio"] is True
    assert made.labels == ["ai_generated"]


def test_three_reasons_speaks_the_three_reasons(auth_client: Any, oil: Any, lina: Any) -> None:
    made = render(auth_client, talking(oil, lina, "three-reasons"))
    for reason in ("Cold-pressed", "Keeps for months", "Tastes green"):
        assert f'says: "{reason}"' in made.prompt_sent, made.prompt_sent
    assert "reason 1" not in made.prompt_sent.lower()


def test_a_template_without_speech_still_asks_for_none(auth_client: Any, oil: Any) -> None:
    from ai.tests.test_ad_templates import template_clip

    made = render(auth_client, template_clip(oil, template="pour-shot"))
    assert "says:" not in made.prompt_sent


# -----------------------------------------------------------------------------
# Refused before anything is charged
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "script", "line"),
    [
        (
            "three-reasons",
            {"hook": "Three reasons.", "body": "Cold-pressed", "cta": "Go."},
            "claim2",
        ),
        ("founder-story", {"hook": "", "body": "Now I use it daily.", "cta": "Try it."}, "hook"),
        ("comment-reply", {"hook": "Worth it?", "body": "  ", "cta": "Ask me."}, "body"),
    ],
)
def test_a_missing_line_is_refused_before_any_charge(
    key: str, script: dict[str, str], line: str, auth_client: Any, oil: Any, lina: Any
) -> None:
    units = ledger.video_balance(oil.workspace)
    response = auth_client.post(ESTIMATE, talking(oil, lina, key, script=script), format="json")
    assert response.status_code == 400, response.json()
    assert code_of(response) == "dialogue_required"
    assert response.json()["error"]["detail"]["line"] == line
    assert not VideoRender.objects.exists()
    assert ledger.video_balance(oil.workspace) == units


def test_a_line_too_long_for_its_beat_is_refused(auth_client: Any, oil: Any, lina: Any) -> None:
    long_hook = " ".join(["word"] * 30)
    response = auth_client.post(
        ESTIMATE,
        talking(oil, lina, "founder-story", script={**LINES["founder-story"], "hook": long_hook}),
        format="json",
    )
    assert response.status_code == 400
    assert code_of(response) == "dialogue_too_long"
    detail = response.json()["error"]["detail"]
    assert detail["line"] == "hook" and detail["words"] == 30 and detail["max"] < 30


def test_a_model_that_cannot_speak_is_refused_before_any_charge(
    auth_client: Any, oil: Any, lina: Any
) -> None:
    VideoModelProfile.objects.update(audio=False)
    response = auth_client.post(ESTIMATE, talking(oil, lina, "comment-reply"), format="json")
    assert response.status_code == 422
    assert code_of(response) == "route_cannot"
    assert response.json()["error"]["detail"]["needs"] == ["SP"]


def test_another_organizations_avatar_is_404(
    auth_client: Any, oil: Any, other_user: Any, make_png_upload: Any
) -> None:
    theirs = avatar_for(provision_workspace(other_user, name="Stranger"), make_png_upload)
    response = auth_client.post(ESTIMATE, talking(oil, theirs, "three-reasons"), format="json")
    assert response.status_code == 404


# -----------------------------------------------------------------------------
# Music: the composer's bed, only when picked
# -----------------------------------------------------------------------------
def test_music_is_mixed_by_the_composer_only_when_picked(
    auth_client: Any, oil: Any, lina: Any
) -> None:
    track = CreativeOption.objects.filter(kind=CreativeKind.MUSIC, is_active=True).first()
    assert track is not None
    _fake_video_composer.clear()
    with_music = render(auth_client, talking(oil, lina, "comment-reply", music=track.key))
    assert _fake_video_composer.calls[-1]["music"] == track.key
    assert "no music" in with_music.prompt_sent.lower()  # never asked of the model
    _fake_video_composer.clear()
    render(auth_client, talking(oil, lina, "comment-reply"))
    assert "music" not in _fake_video_composer.calls[-1]


def test_an_unknown_track_is_400(auth_client: Any, oil: Any, lina: Any) -> None:
    response = auth_client.post(
        ESTIMATE, talking(oil, lina, "comment-reply", music="no-such-track"), format="json"
    )
    assert response.status_code == 400


# -----------------------------------------------------------------------------
# The prompt check and the data
# -----------------------------------------------------------------------------
def test_the_check_refuses_speech_told_to_be_silent_or_an_empty_line() -> None:
    assert prompt_lint.problems('0-2 s: says: "Hi". Sound: no speech.', person=True)
    assert prompt_lint.problems('0-2 s: says: "".', person=True)
    assert prompt_lint.problems('0-2 s: says: "Hi there". Sound: no music.', person=True) == []


def test_speech_is_found_in_a_direction(catalog: None) -> None:
    rows = {
        row.key: row
        for row in CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, key__in=TALKING)
    }
    for key in TALKING:
        assert video_prompt.speaks(rows[key].metadata["direction"]), key
    pour = CreativeOption.objects.get(kind=CreativeKind.AD_TEMPLATE, key="pour-shot")
    assert not video_prompt.speaks(pour.metadata["direction"])


def test_the_migration_turns_on_seeded_rows_and_keeps_an_operators_own(
    catalog: None, settings: Any
) -> None:
    migration = importlib.import_module("ai.migrations.0057_talking_templates")
    settings.SEED_CATALOG_ON_MIGRATE = True
    rows = {
        row.key: row
        for row in CreativeOption.objects.filter(kind=CreativeKind.AD_TEMPLATE, key__in=TALKING)
    }
    for row in rows.values():  # as an older database holds them
        row.metadata = {**row.metadata, "available": False, "lengths": ["clip-15"]}
        row.description = migration.OLD_DESCRIPTION
        row.label = "Founder story" if row.key == "founder-story" else row.label
        row.save(update_fields=["metadata", "description", "label"])
    ours = rows["comment-reply"]
    ours.description = "Our own words"
    ours.save(update_fields=["description"])
    grok = VideoModelProfile.objects.get(slug="x-ai/grok-imagine-video-1.5-lite")
    grok.audio = False
    grok.save(update_fields=["audio"])

    migration.forwards(apps, None)

    for key in ("founder-story", "three-reasons"):
        rows[key].refresh_from_db()
        assert rows[key].metadata["available"] is True
        assert rows[key].metadata["lengths"] == ["clip-10"]
    rows["founder-story"].refresh_from_db()
    assert rows["founder-story"].label == "Happy customer"
    ours.refresh_from_db()
    assert ours.metadata["available"] is False  # an operator's own row is left alone
    grok.refresh_from_db()
    assert grok.audio is True
