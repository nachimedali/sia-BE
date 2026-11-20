"""S19-01 video model profiles and S19-08 Pro routes (`upgrade_rendering.md` §6.2, §10).

- the seed lands every profile, a second run adds nothing, an admin edit stays;
- an unknown slug gets the cautious profile: no last frame, no seed, no audio;
- every admin save is recorded;
- a flagged route is resolved only with its flag on, and then before the
  unflagged row of its scope; Growth and Scale move, the other packages never.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import RequestFactory

from ai.admin import VideoModelProfileAdmin
from ai.models import AIModelRoute, AIModelRouteChange, AITask, VideoModelProfile
from ai.route_seed import KLING_3_PRO, OMNI_FLASH, PRO_FLAG
from ai.services.routing import resolve, resolve_for
from billing.models import FeatureFlag
from billing.services.flags import PRO_RENDERING_S19

pytestmark = pytest.mark.django_db

SEED = import_module("ai.migrations.0050_seed_video_profiles_and_pro_routes")


@pytest.fixture
def seeded(settings, plans) -> None:
    settings.SEED_CATALOG_ON_MIGRATE = True
    from django.apps import apps

    SEED.forwards(apps, None)


def _on(organization: Any) -> None:
    FeatureFlag.objects.update_or_create(
        organization=organization, key=PRO_RENDERING_S19, defaults={"enabled": True}
    )


def test_the_seed_lands_every_profile_once_and_never_overwrites_an_edit(seeded) -> None:
    from django.apps import apps

    from ai.video_profile_seed import PROFILES

    assert VideoModelProfile.objects.count() == len(PROFILES)
    VideoModelProfile.objects.filter(slug=OMNI_FLASH).update(budget_words=90)
    SEED.forwards(apps, None)

    assert VideoModelProfile.objects.count() == len(PROFILES)
    assert VideoModelProfile.objects.get(slug=OMNI_FLASH).budget_words == 90
    for profile in VideoModelProfile.objects.all():
        profile.full_clean()


def test_omni_lands_seeds_and_speaks_and_grok_lite_only_speaks(seeded) -> None:
    omni = VideoModelProfile.for_slug(OMNI_FLASH)
    grok = VideoModelProfile.for_slug("x-ai/grok-imagine-video-1.5-lite")

    assert (omni.last_frame, omni.seed, omni.audio) == (True, True, True)
    # Sound: OpenRouter's `generate_audio: true` (2026-11-19) and the S17-12 lip-sync pass.
    assert (grok.last_frame, grok.seed, grok.audio) == (False, False, True)
    assert VideoModelProfile.for_slug(KLING_3_PRO).seed is False


def test_an_unknown_slug_is_treated_cautiously() -> None:
    profile = VideoModelProfile.for_slug("vendor/never-heard-of-it")

    assert not profile.pk  # never saved
    assert (profile.last_frame, profile.seed, profile.audio) == (False, False, False)
    assert profile.timecodes == "seconds"


def test_a_length_is_fitted_to_what_the_model_accepts() -> None:
    veo = VideoModelProfile(slug="veo", durations=[4, 6, 8])

    assert [veo.fit_seconds(n) for n in (2, 4, 5, 7.5, 12)] == [4, 4, 6, 8, 8]


def test_a_profile_refuses_lengths_that_are_not_whole_seconds() -> None:
    with pytest.raises(ValidationError):
        VideoModelProfile(slug="x", durations=[]).full_clean()
    with pytest.raises(ValidationError):
        VideoModelProfile(slug="x", durations=[5, 2.5]).full_clean()
    with pytest.raises(ValidationError):
        VideoModelProfile(slug="x", durations=[5], budget_words=10).full_clean()


def test_every_admin_save_of_a_profile_is_recorded(user) -> None:
    request = RequestFactory().post("/")
    request.user = user
    admin = VideoModelProfileAdmin(VideoModelProfile, AdminSite())
    profile = VideoModelProfile(slug="m/one", durations=[5])
    admin.save_model(request, profile, form=None, change=False)
    profile.audio = True
    admin.save_model(request, profile, form=None, change=True)

    changes = list(AIModelRouteChange.objects.filter(profile=profile).order_by("created_at"))
    assert [c.before.get("audio") for c in changes] == [None, False]
    assert changes[-1].after["audio"] is True
    assert changes[-1].actor == user
    assert admin.has_delete_permission(request, profile) is False


# -----------------------------------------------------------------------------
# S19-08: the Pro routes
# -----------------------------------------------------------------------------
def test_pro_routes_wait_for_the_flag(seeded, workspace, plans) -> None:
    organization = workspace.organization
    for code in ("growth", "scale"):
        organization.plan = plans[code]
        organization.save(update_fields=["plan"])
        for task in (AITask.VIDEO_START, AITask.VIDEO_END):
            assert resolve_for(task, workspace).model != OMNI_FLASH, (code, task)

    _on(organization)
    for code in ("growth", "scale"):
        organization.plan = plans[code]
        organization.save(update_fields=["plan"])
        for task in (AITask.VIDEO_START, AITask.VIDEO_END):
            route = resolve_for(task, workspace)
            assert (route.model, route.fallback_model) == (OMNI_FLASH, KLING_3_PRO), code
            assert route.params == {"resolution": "1080p", "audio": True, "fallback_audio": False}


def test_the_lower_packages_never_move(seeded, workspace, plans) -> None:
    from django.apps import apps

    import_module("ai.migrations.0021_seed_model_routes").forwards(apps, None)
    import_module("ai.migrations.0024_spark_wan_routes").forwards(apps, None)
    organization = workspace.organization
    before = {}
    for code in ("spark", "discovery", "essential"):
        organization.plan = plans[code]
        organization.save(update_fields=["plan"])
        before[code] = resolve_for(AITask.VIDEO_START, workspace)
    _on(organization)
    for code in ("spark", "discovery", "essential"):
        organization.plan = plans[code]
        organization.save(update_fields=["plan"])
        assert resolve_for(AITask.VIDEO_START, workspace) == before[code], code


def test_a_flagged_row_is_never_resolved_without_an_organization(seeded, plans) -> None:
    assert resolve(AITask.VIDEO_START, plan=plans["growth"]).model != OMNI_FLASH


def test_a_route_naming_an_unknown_flag_is_refused(plans) -> None:
    route = AIModelRoute(
        task=AITask.VIDEO_START,
        plan=plans["growth"],
        provider="fake",
        model="m",
        flag="no_such_flag",
    )
    with pytest.raises(ValidationError) as caught:
        route.clean()
    assert "flag" in caught.value.message_dict


def test_the_fallback_renders_silent_to_stay_inside_the_unit(
    seeded, workspace, plans, monkeypatch
) -> None:
    from ai.providers.fake import _fake_video_provider
    from ai.providers.video import get_video_provider
    from common.exceptions import ProviderError

    organization = workspace.organization
    organization.plan = plans["growth"]
    organization.save(update_fields=["plan"])
    _on(organization)
    _fake_video_provider.clear()
    real = _fake_video_provider.animate

    def omni_down(**kwargs: Any) -> Any:
        if kwargs["model"] == OMNI_FLASH:
            raise ProviderError("down", detail={"status": 500})
        return real(**kwargs)

    monkeypatch.setattr(_fake_video_provider, "animate", omni_down)
    provider = get_video_provider(workspace=workspace)
    assert provider is not None
    provider.animate(image=b"x", role="start", prompt="p", aspect="9:16", duration_seconds=10)

    [request] = _fake_video_provider.requests
    assert (request["model"], request["audio"]) == (KLING_3_PRO, False)


def test_the_flag_is_off_by_default() -> None:
    from billing.services.flags import ROLLOUT_DEFAULTS

    assert ROLLOUT_DEFAULTS[PRO_FLAG] is False
