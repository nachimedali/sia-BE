"""The Studio's Motion step (steps-plan S3): animate a still, build a reel,
hold the price, debit once on pass, send to a post through the normal path."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from ai.creative_seed import CATALOG
from ai.models import CreativeOption, VideoRender, VideoRenderStatus, VideoReview
from ai.providers.base import VideoResult
from ai.providers.fake import _fake_video_composer, _fake_video_provider, read_fake_manifest
from ai.services import video
from ai.video_seed import VIDEO_CATALOG
from billing.models import CreditLedger, FeatureFlag
from billing.services import ledger
from billing.services.flags import VIDEO_S3
from common.exceptions import FeatureNotAvailable, ProviderError, StateConflict
from content.models import MediaKind, PostStatus
from content.services.media import ingest_media
from products.models import Product
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

BASE = "/api/v1/ai/video/"
CATALOGUE = f"{BASE}catalogue/"
ESTIMATE = f"{BASE}estimate/"
RENDERS = f"{BASE}renders/"
SEND = f"{RENDERS}send/"


@pytest.fixture
def catalogue(db: None) -> None:
    rows = {**VIDEO_CATALOG, "language": CATALOG["language"], "cta": CATALOG["cta"]}
    for kind, specs in rows.items():
        for position, spec in enumerate(specs):
            CreativeOption.objects.create(kind=kind, sort_order=position, **spec)


@pytest.fixture
def pro(paid_workspace: Any, catalogue: None) -> Any:
    ledger.grant_credits(paid_workspace, 100)
    return paid_workspace


@pytest.fixture
def still(pro: Any, make_png_upload: Any) -> Any:
    return ingest_media(workspace=pro, upload=make_png_upload("bottle.png", (600, 750)))


@pytest.fixture
def stills(pro: Any, make_png_upload: Any) -> list[Any]:
    return [
        ingest_media(workspace=pro, upload=make_png_upload(f"shot-{i}.png", (600, 600 + i * 10)))
        for i in range(4)
    ]


def clip(still: Any, **overrides: Any) -> dict[str, Any]:
    return {
        "mode": "clip",
        "length": "clip-5",
        "source": still.pk,
        "role": "start",
        "motion": "push",
        "aspect": "9-16",
        "text": "First harvest, pressed this morning.",
        **overrides,
    }


def reel(stills: list[Any], **overrides: Any) -> dict[str, Any]:
    return {
        "mode": "reel",
        "length": "reel-15",
        "shots": [{"media": s.pk, "text": f"Line {i}"} for i, s in enumerate(stills)],
        "style": "reveal",
        "music": "oud",
        "captions": True,
        "overlays": True,
        "end_card": True,
        "cta": "shop-now",
        "language": "fr",
        "text": "Cold pressed in Djerba from olives picked this week, bottled by hand and numbered",
        **overrides,
    }


def confirmed(client: Any, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(ESTIMATE, body, format="json")
    assert response.status_code == 200, response.json()
    return {**body, "estimate_id": response.json()["estimate_id"]}


def balance(workspace: Any) -> int:
    return ledger.credit_balance(workspace)


# --- the catalogue ----------------------------------------------------------------------
def test_the_catalogue_serves_every_video_control_with_its_prices(
    auth_client: Any, pro: Any
) -> None:
    body = auth_client.get(CATALOGUE).json()

    assert [row["key"] for row in body["lengths"]] == ["clip-5", "clip-10", "reel-15", "reel-30"]
    assert body["lengths"][0]["metadata"]["credits"] == 25
    assert {row["key"] for row in body["motions"]} >= {"push", "orbit", "light-sweep"}
    assert [row["key"] for row in body["aspects"]] == ["9-16", "4-5", "1-1"]
    assert "prompt_fragment" not in body["motions"][0]  # instruction to a model, not a tile
    assert body["version"]


def test_a_retired_row_leaves_the_catalogue(auth_client: Any, pro: Any) -> None:
    CreativeOption.objects.filter(kind="motion", key="orbit").update(is_active=False)

    keys = [row["key"] for row in auth_client.get(CATALOGUE).json()["motions"]]

    assert "orbit" not in keys


# --- estimates --------------------------------------------------------------------------
def test_a_clip_is_priced_from_its_length_and_motion(auth_client: Any, still: Any) -> None:
    plain = auth_client.post(ESTIMATE, clip(still), format="json").json()
    orbit = auth_client.post(ESTIMATE, clip(still, motion="orbit"), format="json").json()

    assert plain["lines"] == [{"label": "5 s clip", "credits": 25}]
    assert plain["credits"] == 25 and plain["render_s"] == 12
    assert orbit["lines"][-1] == {"label": "Half orbit", "credits": 2}
    assert orbit["credits"] == 27
    assert plain["estimate_id"] != orbit["estimate_id"]
    # The same request is the same estimate.
    again = auth_client.post(ESTIMATE, clip(still), format="json").json()
    assert again["estimate_id"] == plain["estimate_id"]


def test_a_reel_is_priced_per_shot_style_music_and_captions(
    auth_client: Any, stills: list[Any]
) -> None:
    body = auth_client.post(ESTIMATE, reel(stills), format="json").json()

    assert body["lines"] == [
        {"label": "15 s reel", "credits": 12},
        {"label": "4 shots", "credits": 4},
        {"label": "Hero reveal", "credits": 1},
        {"label": "Music bed · Oud lounge", "credits": 1},
        {"label": "Burned-in captions", "credits": 1},
    ]
    assert body["credits"] == 19
    assert body["render_s"] == 20 + 4


def test_a_price_retuned_in_admin_is_the_price_quoted(auth_client: Any, still: Any) -> None:
    row = CreativeOption.objects.get(kind="video_length", key="clip-5")
    row.metadata = {**row.metadata, "credits": 9}
    row.save()

    assert auth_client.post(ESTIMATE, clip(still), format="json").json()["credits"] == 9


def test_a_priced_row_without_a_price_is_an_error_never_free(auth_client: Any, still: Any) -> None:
    row = CreativeOption.objects.get(kind="video_length", key="clip-5")
    row.metadata = {k: v for k, v in row.metadata.items() if k != "credits"}
    row.save()

    response = auth_client.post(ESTIMATE, clip(still), format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "video_price_not_configured"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"motion": "teleport"}, "motion"),
        ({"length": "reel-15"}, "length"),
        ({"aspect": "3-2"}, "aspect"),
        ({"source": None}, "source"),
        ({"language": "de"}, "language"),
    ],
)
def test_a_clip_outside_the_catalogue_is_a_400(
    auth_client: Any, still: Any, change: dict[str, Any], field: str
) -> None:
    response = auth_client.post(ESTIMATE, clip(still, **change), format="json")

    assert response.status_code == 400
    assert field in str(response.json()["error"])


def test_a_reel_needs_three_to_five_shots(auth_client: Any, stills: list[Any]) -> None:
    short = auth_client.post(ESTIMATE, reel(stills[:2]), format="json")
    long = auth_client.post(ESTIMATE, reel([*stills, *stills[:2]]), format="json")

    assert short.status_code == long.status_code == 400
    assert "3 to 5" in str(short.json()["error"])


def test_captions_need_post_text(auth_client: Any, stills: list[Any]) -> None:
    response = auth_client.post(ESTIMATE, reel(stills, text=""), format="json")

    assert response.status_code == 400
    assert "text" in str(response.json()["error"])


def test_only_an_image_can_be_animated(auth_client: Any, pro: Any) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    clip_file = ingest_media(
        workspace=pro,
        upload=SimpleUploadedFile("a.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4"),
    )
    assert clip_file.kind == MediaKind.VIDEO

    response = auth_client.post(ESTIMATE, {**clip(clip_file)}, format="json")

    assert response.status_code == 400


def test_a_plan_without_video_hears_402_upgrade(
    auth_client: Any, workspace: Any, catalogue: None, make_png_upload: Any
) -> None:
    # Spark carries a free allowance; with none, there is no video at all.
    plan = workspace.organization.plan
    plan.features = {**plan.features, "free_video_clips": 0}
    plan.save()
    asset = ingest_media(workspace=workspace, upload=make_png_upload())

    response = auth_client.post(ESTIMATE, clip(asset), format="json")

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "feature_not_available"
    assert response.json()["error"]["upgrade"]


def test_no_vendor_configured_is_a_plain_422(auth_client: Any, still: Any, settings: Any) -> None:
    settings.USE_FAKE_AI_PROVIDERS = False
    settings.VIDEO_PROVIDER_API_KEY = ""

    response = auth_client.post(ESTIMATE, clip(still), format="json")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "video_unavailable"


# --- rendering a clip -------------------------------------------------------------------
def test_a_confirmed_clip_renders_into_a_derived_video_and_is_charged_once(
    auth_client: Any, pro: Any, still: Any
) -> None:
    before = balance(pro)

    response = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "SUCCEEDED" and body["phase"] == "DONE" and body["progress"] == 1
    assert body["charged"] == body["credits"] == 25
    assert body["stills"][0]["id"] == still.pk and body["stills"][0]["url"]
    output = body["output"]
    assert output["kind"] == "VIDEO" and output["derived_from"] == still.pk
    assert output["duration_ms"] == 5000 and (output["width"], output["height"]) == (1080, 1920)
    assert balance(pro) == before - 25
    render = VideoRender.objects.get(pk=body["id"])
    assert render.charge is not None and render.charge.delta == -25
    assert CreditLedger.objects.filter(workspace=pro, note__startswith="video render").count() == 1
    # The provider was told how the camera moves — the motion row, rewritten
    # into one brief (the fake text model keeps the row's words).
    push = CreativeOption.objects.get(kind="motion", key="push").prompt_fragment
    assert _fake_video_provider.calls == [render.rephrased_prompt]
    assert push.rstrip(".").lower() in render.rephrased_prompt.lower()
    manifest = read_fake_manifest(render.output.file.open("rb").read())  # type: ignore[union-attr]
    assert manifest["role"] == "start" and manifest["aspect"] == "9:16"


def test_a_retried_task_neither_renders_nor_charges_twice(
    auth_client: Any, pro: Any, still: Any
) -> None:
    body = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()
    after_first = balance(pro)

    video.run_render(body["id"])
    video.run_render(body["id"])

    assert balance(pro) == after_first
    assert len(_fake_video_provider.calls) == 1


def test_a_price_change_after_confirming_is_a_409_with_the_new_estimate(
    auth_client: Any, pro: Any, still: Any
) -> None:
    request = confirmed(auth_client, clip(still))
    row = CreativeOption.objects.get(kind="video_length", key="clip-5")
    row.metadata = {**row.metadata, "credits": 7}
    row.save()

    response = auth_client.post(RENDERS, request, format="json")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "estimate_changed"
    assert error["detail"]["estimate"]["credits"] == 7
    assert not VideoRender.objects.exists()


def test_a_render_in_flight_holds_its_credits(auth_client: Any, pro: Any, still: Any) -> None:
    VideoRender.objects.create(
        workspace=pro,
        created_by=pro.organization.owner,
        mode="clip",
        spec={},
        estimate_id="est_x",
        credits=96,
    )

    listed = auth_client.get(RENDERS).json()
    response = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json")

    assert listed["credits"] == {"balance": 100, "held": 96}
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "insufficient_credits"
    assert error["detail"] == {"required": 25, "available": 4, "held": 96}
    assert "held" in error["message"]


def test_a_render_that_fails_the_gate_takes_nothing(
    auth_client: Any, pro: Any, still: Any, monkeypatch: Any
) -> None:
    def short(**kwargs: Any) -> VideoResult:
        return VideoResult(
            content=b"\x00\x00\x00\x18ftypmp42" + b"x" * 40,
            mime="video/mp4",
            duration_seconds=2.0,
            provider="fake",
            model="fake-video-1",
            latency_ms=1,
        )

    monkeypatch.setattr(_fake_video_provider, "animate", short)
    before = balance(pro)

    body = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()

    assert body["status"] == "FAILED" and body["error_code"] == "quality_gate_failed"
    assert "2.0 s long instead of 5 s" in body["error"] and "No credits" in body["error"]
    assert body["charged"] == 0 and body["output"] is None
    assert balance(pro) == before
    assert auth_client.get(RENDERS).json()["credits"]["held"] == 0  # the hold is released


def test_a_provider_error_fails_the_render_without_charging(
    auth_client: Any, pro: Any, still: Any, monkeypatch: Any
) -> None:
    def broken(**kwargs: Any) -> VideoResult:
        raise ProviderError("vendor down")

    monkeypatch.setattr(_fake_video_provider, "animate", broken)
    before = balance(pro)

    body = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()

    assert body["status"] == "FAILED" and body["error_code"] == "provider_error"
    assert balance(pro) == before


def test_credits_gone_by_the_time_it_finishes_fail_it_unpaid(
    pro: Any, still: Any, user: Any
) -> None:
    spec = video.parse(pro, clip(still))
    priced = video.quote(spec)
    render = VideoRender.objects.create(
        workspace=pro,
        created_by=user,
        mode="clip",
        spec=spec.stored(),
        source=still,
        estimate_id=priced.estimate_id,
        credits=priced.credits,
    )
    ledger.adjust_credits(pro, -balance(pro), actor=user, note="drained in a test")

    result = video.run_render(render.pk)

    assert result.status == VideoRenderStatus.FAILED
    assert result.error_code == "insufficient_credits"
    assert result.output is None and balance(pro) == 0


# --- reels ------------------------------------------------------------------------------
def test_a_reel_is_cut_with_overlays_music_captions_and_an_end_card(
    auth_client: Any, pro: Any, stills: list[Any]
) -> None:
    product = Product.objects.create(workspace=pro, name="First harvest EVOO")
    body = auth_client.post(
        RENDERS, confirmed(auth_client, reel(stills, product=product.pk)), format="json"
    ).json()

    assert body["status"] == "SUCCEEDED" and body["charged"] == 19
    assert [s["text"] for s in body["stills"]] == ["Line 0", "Line 1", "Line 2", "Line 3"]
    assert body["output"]["derived_from"] == stills[0].pk
    (call,) = _fake_video_composer.calls
    assert call["aspect"] == "9:16" and call["seconds"] == 15
    assert call["transition"] == "build" and call["music"] == "oud"
    assert [shot["overlay"] for shot in call["shots"]] == ["Line 0", "Line 1", "Line 2", "Line 3"]
    # 13 s of shots and a 2 s end card.
    assert [shot["seconds"] for shot in call["shots"]] == [3.25] * 4
    assert call["end_card"] == {"title": "First harvest EVOO", "cta": "Shop now", "seconds": 2.0}
    # Burned-in captions are the post text, in five-word cards over the shots.
    assert call["captions"][0] == ["Cold pressed in Djerba from", 0.0, 4.33]
    assert call["captions"][-1][2] == 13.0
    assert " ".join(cue[0] for cue in call["captions"]) == reel(stills)["text"]


def test_overlays_off_cuts_the_shots_clean(auth_client: Any, pro: Any, stills: list[Any]) -> None:
    body = reel(stills, overlays=False, captions=False, end_card=False, music="none")
    auth_client.post(RENDERS, confirmed(auth_client, body), format="json")

    (call,) = _fake_video_composer.calls
    assert {shot["overlay"] for shot in call["shots"]} == {""}
    assert call["captions"] == [] and call["end_card"] is None and call["music"] is None
    assert [shot["seconds"] for shot in call["shots"]] == [3.75] * 4


def test_caption_cues_are_capped_and_timed_in_code() -> None:
    text = " ".join(f"w{i}" for i in range(60))

    cues = video.caption_cues(text, start=0.0, end=8.0)

    assert len(cues) == video.CAPTION_MAX_CUES
    assert cues[0].text == "w0 w1 w2 w3 w4" and (cues[0].start, cues[0].end) == (0.0, 1.0)
    assert cues[-1].end == 8.0
    assert video.caption_cues("", start=0, end=8) == []


# --- review, retry, send ----------------------------------------------------------------
def test_accept_discard_and_undo(auth_client: Any, pro: Any, still: Any) -> None:
    rid = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()["id"]

    accepted = auth_client.patch(f"{RENDERS}{rid}/", {"review": "ACCEPTED"}, format="json")
    undone = auth_client.patch(f"{RENDERS}{rid}/", {"review": ""}, format="json")
    hidden = auth_client.patch(f"{RENDERS}{rid}/", {"review": "HIDDEN"}, format="json")

    assert accepted.json()["review"] == "ACCEPTED"
    assert undone.json()["review"] == ""
    assert hidden.status_code == 200
    assert rid not in [r["id"] for r in auth_client.get(RENDERS).json()["renders"]]


def test_a_render_still_in_flight_cannot_be_reviewed(auth_client: Any, pro: Any) -> None:
    render = VideoRender.objects.create(
        workspace=pro,
        created_by=pro.organization.owner,
        mode="clip",
        spec={},
        estimate_id="est_x",
        credits=1,
    )

    response = auth_client.patch(f"{RENDERS}{render.pk}/", {"review": "ACCEPTED"}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "render_not_reviewable"


def test_a_failed_render_is_retried_as_a_new_one(
    auth_client: Any, pro: Any, still: Any, monkeypatch: Any
) -> None:
    def broken(**kwargs: Any) -> VideoResult:
        raise ProviderError("vendor down")

    with monkeypatch.context() as patch:
        patch.setattr(_fake_video_provider, "animate", broken)
        failed = auth_client.post(
            RENDERS, confirmed(auth_client, clip(still)), format="json"
        ).json()

    retry = auth_client.post(
        RENDERS, {**confirmed(auth_client, clip(still)), "retry_of": failed["id"]}, format="json"
    )

    assert retry.status_code == 202 and retry.json()["status"] == "SUCCEEDED"
    assert retry.json()["retry_of"] == failed["id"]
    assert VideoRender.objects.get(pk=failed["id"]).review == VideoReview.RETRIED
    # Only a failure can be retried.
    again = auth_client.post(
        RENDERS,
        {**confirmed(auth_client, clip(still)), "retry_of": retry.json()["id"]},
        format="json",
    )
    assert again.status_code == 409


def test_an_accepted_clip_lands_on_a_draft_post_through_the_normal_path(
    auth_client: Any, pro: Any, still: Any, user: Any
) -> None:
    rid = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()["id"]
    auth_client.patch(f"{RENDERS}{rid}/", {"review": "ACCEPTED"}, format="json")

    response = auth_client.post(SEND, {"renders": [rid], "platforms": ["instagram"]}, format="json")

    assert response.status_code == 201
    (post_id,) = response.json()["posts"]
    render = VideoRender.objects.get(pk=rid)
    post = render.post
    assert post is not None and post.pk == post_id
    assert post.status == PostStatus.DRAFT and post.source == "AI"
    assert post.master_body == "First harvest, pressed this morning."
    assert list(post.media_assets.values_list("pk", flat=True)) == [render.output_id]
    assert post.planned_platforms == ["instagram"]
    assert not post.targets.exists()  # nothing reaches a platform from here
    # Sent is final: not again, and not un-accepted.
    assert auth_client.post(SEND, {"renders": [rid]}, format="json").status_code == 409
    assert auth_client.patch(f"{RENDERS}{rid}/", {"review": ""}, format="json").status_code == 409


def test_the_sent_clip_still_needs_approval_before_it_can_be_scheduled(
    auth_client: Any, advanced_workspace: Any, catalogue: None, make_png_upload: Any, user: Any
) -> None:
    from scheduling.services import schedule_post

    ledger.grant_credits(advanced_workspace, 50)
    asset = ingest_media(workspace=advanced_workspace, upload=make_png_upload())
    rid = auth_client.post(RENDERS, confirmed(auth_client, clip(asset)), format="json").json()["id"]
    auth_client.patch(f"{RENDERS}{rid}/", {"review": "ACCEPTED"}, format="json")
    (post_id,) = auth_client.post(SEND, {"renders": [rid]}, format="json").json()["posts"]
    post = VideoRender.objects.get(pk=rid).post
    assert post is not None and post.pk == post_id

    with pytest.raises(StateConflict):
        schedule_post(
            post=post,
            delivery_mode="AUTO_PUBLISH",
            scheduled_at=timezone.now() + dt.timedelta(days=1),
            actor=user,
        )
    # Debited once for the whole journey.
    assert (
        CreditLedger.objects.filter(
            workspace=advanced_workspace, note__startswith="video render"
        ).count()
        == 1
    )


def test_only_accepted_renders_are_sent(auth_client: Any, pro: Any, still: Any) -> None:
    rid = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()["id"]

    response = auth_client.post(SEND, {"renders": [rid]}, format="json")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "render_not_reviewable"


def test_a_render_for_a_post_is_tied_to_it_and_never_sent_elsewhere(
    auth_client: Any, pro: Any, still: Any, user: Any
) -> None:
    from content.services.posts import create_post

    post = create_post(workspace=pro, author=user, master_body="Draft", media_assets=[still])

    body = auth_client.post(
        RENDERS, {**confirmed(auth_client, clip(still)), "post": post.pk}, format="json"
    ).json()

    assert body["post"] == post.pk
    assert (
        auth_client.patch(
            f"{RENDERS}{body['id']}/", {"review": "ACCEPTED"}, format="json"
        ).status_code
        == 409
    )


# --- tenancy, permissions, the flag -------------------------------------------------------
def test_another_organizations_render_still_and_post_are_404(
    auth_client: Any, pro: Any, still: Any, other_user: Any, make_png_upload: Any
) -> None:
    from content.services.posts import create_post

    theirs = provision_workspace(other_user, name="Theirs")
    their_still = ingest_media(workspace=theirs, upload=make_png_upload())
    their_render = VideoRender.objects.create(
        workspace=theirs, created_by=other_user, mode="clip", spec={}, estimate_id="e", credits=1
    )
    their_post = create_post(workspace=theirs, author=other_user, master_body="x")

    assert auth_client.get(f"{RENDERS}{their_render.pk}/").status_code == 404
    assert (
        auth_client.patch(f"{RENDERS}{their_render.pk}/", {"review": ""}, format="json").status_code
        == 404
    )
    assert auth_client.post(ESTIMATE, clip(their_still), format="json").status_code == 404
    assert auth_client.post(SEND, {"renders": [their_render.pk]}, format="json").status_code == 404
    request = confirmed(auth_client, clip(still))
    assert (
        auth_client.post(RENDERS, {**request, "post": their_post.pk}, format="json").status_code
        == 404
    )
    assert (
        auth_client.post(
            RENDERS, {**request, "retry_of": their_render.pk}, format="json"
        ).status_code
        == 404
    )
    assert their_render.pk not in [r["id"] for r in auth_client.get(RENDERS).json()["renders"]]


def test_a_sibling_workspaces_render_and_still_are_404(
    auth_client: Any, pro: Any, user: Any, make_png_upload: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Second Brand")
    their_still = ingest_media(workspace=sibling, upload=make_png_upload())
    render = VideoRender.objects.create(
        workspace=sibling, created_by=user, mode="clip", spec={}, estimate_id="e", credits=1
    )
    here = {"HTTP_X_WORKSPACE_ID": str(pro.pk)}

    assert auth_client.get(f"{RENDERS}{render.pk}/", **here).status_code == 404
    assert auth_client.post(ESTIMATE, clip(their_still), format="json", **here).status_code == 404
    there = {"HTTP_X_WORKSPACE_ID": str(sibling.pk)}
    assert auth_client.get(f"{RENDERS}{render.pk}/", **there).status_code == 200


def test_a_viewer_reads_the_queue_but_cannot_render(
    client_as: Any, viewer_user: Any, catalogue: None
) -> None:
    viewer = client_as(viewer_user)

    assert viewer.get(RENDERS).status_code == 200
    assert (
        viewer.post(ESTIMATE, {"mode": "clip", "length": "clip-5"}, format="json").status_code
        == 403
    )


def test_flag_off_hides_every_video_endpoint(auth_client: Any, pro: Any, still: Any) -> None:
    FeatureFlag.objects.create(organization=pro.organization, key=VIDEO_S3, enabled=False)

    assert auth_client.get(CATALOGUE).status_code == 404
    assert auth_client.post(ESTIMATE, clip(still), format="json").status_code == 404
    assert auth_client.get(RENDERS).status_code == 404
    assert auth_client.post(SEND, {"renders": [1]}, format="json").status_code == 404
    assert auth_client.get(f"{RENDERS}1/").status_code == 404


# --- the post editor's Animate (S3, hardened) -----------------------------------------
@pytest.fixture
def draft(pro: Any, still: Any, user: Any) -> Any:
    from content.services.posts import create_post

    return create_post(workspace=pro, author=user, master_body="Draft", media_assets=[still])


def test_animating_twice_returns_the_same_render_and_charges_once(
    auth_client: Any, pro: Any, still: Any, draft: Any
) -> None:
    request = {**confirmed(auth_client, clip(still)), "post": draft.pk}

    first = auth_client.post(RENDERS, request, format="json").json()
    second = auth_client.post(RENDERS, request, format="json").json()

    assert second["id"] == first["id"]
    assert CreditLedger.objects.filter(workspace=pro, note__startswith="video render").count() == 1
    assert len(_fake_video_provider.calls) == 1
    # A different clip of the same still is a different render.
    other = auth_client.post(
        RENDERS,
        {**confirmed(auth_client, clip(still, motion="orbit")), "post": draft.pk},
        format="json",
    ).json()
    assert other["id"] != first["id"]


def test_the_original_image_is_never_changed(auth_client: Any, still: Any, draft: Any) -> None:
    before = (still.checksum, still.file.name, still.width, still.height)

    body = auth_client.post(
        RENDERS, {**confirmed(auth_client, clip(still)), "post": draft.pk}, format="json"
    ).json()

    still.refresh_from_db()
    assert (still.checksum, still.file.name, still.width, still.height) == before
    assert body["output"]["id"] != still.pk and body["output"]["derived_from"] == still.pk
    assert list(draft.media_assets.values_list("pk", flat=True)) == [
        still.pk
    ]  # staged, not written


def test_a_locked_post_refuses_animate_with_a_409(
    auth_client: Any, pro: Any, still: Any, draft: Any
) -> None:
    draft.locked_at = timezone.now()
    draft.save(update_fields=["locked_at"])
    before = balance(pro)

    response = auth_client.post(
        RENDERS, {**confirmed(auth_client, clip(still)), "post": draft.pk}, format="json"
    )

    assert response.status_code == 409 and response.json()["error"]["code"] == "post_locked"
    assert balance(pro) == before and not VideoRender.objects.exists()


def test_a_published_post_refuses_animate_with_a_409(
    auth_client: Any, still: Any, draft: Any
) -> None:
    draft.status = PostStatus.PUBLISHED
    draft.save(update_fields=["status"])

    response = auth_client.post(
        RENDERS, {**confirmed(auth_client, clip(still)), "post": draft.pk}, format="json"
    )

    assert response.status_code == 409 and response.json()["error"]["code"] == "post_not_editable"


# --- the motion brief, rewritten by the text model ----------------------------------------
MOTION_REWRITE = (
    "The camera pushes in slowly toward the bottle; light glints along the glass "
    "while the label, cap and shape stay exactly as in the still."
)


class ScriptedText:
    def __init__(self, reply: str = MOTION_REWRITE, *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system: str, prompt: str, n: int, model: str | None = None) -> Any:
        from types import SimpleNamespace

        self.calls.append({"system": system, "prompt": prompt})
        if self.error:
            raise self.error
        return SimpleNamespace(variants=[SimpleNamespace(body=self.reply)], model="nv-model")


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> ScriptedText:
    from ai.services import rephrase

    model = ScriptedText()
    monkeypatch.setattr(rephrase, "get_text_provider", lambda *_a, **_k: model)
    return model


def test_a_clip_animates_with_the_rewritten_motion_brief(
    auth_client: Any, pro: Any, still: Any, scripted: ScriptedText
) -> None:
    body = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()

    render = VideoRender.objects.get(pk=body["id"])
    assert render.rephrased_prompt == MOTION_REWRITE and render.rephrased_by == "nv-model"
    assert _fake_video_provider.calls == [MOTION_REWRITE]
    system = scripted.calls[0]["system"].lower()
    assert "one simple" in system and "verbs" in system and "never redesign" in system
    asked = scripted.calls[0]["prompt"]
    assert "start frame" in asked
    assert CreativeOption.objects.get(kind="motion", key="push").prompt_fragment in asked


def test_a_motion_rewrite_with_an_invented_number_falls_back_to_the_row(
    auth_client: Any, pro: Any, still: Any, scripted: ScriptedText
) -> None:
    scripted.reply = MOTION_REWRITE + " Loved by 40000 customers."

    body = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()

    render = VideoRender.objects.get(pk=body["id"])
    push = CreativeOption.objects.get(kind="motion", key="push").prompt_fragment
    assert render.rephrased_by == "template"
    assert _fake_video_provider.calls == [push]


def test_flag_off_animates_with_the_motion_row_unchanged(
    auth_client: Any, pro: Any, still: Any, scripted: ScriptedText
) -> None:
    from billing.services.flags import STUDIO_REPHRASE

    FeatureFlag.objects.create(organization=pro.organization, key=STUDIO_REPHRASE, enabled=False)

    body = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()

    render = VideoRender.objects.get(pk=body["id"])
    push = CreativeOption.objects.get(kind="motion", key="push").prompt_fragment
    assert scripted.calls == []
    assert render.rephrased_prompt == "" and render.rephrased_by == ""
    assert _fake_video_provider.calls == [push]


# --- the scene controls carry into a video --------------------------------------------
@pytest.fixture
def looks(catalogue: None) -> None:
    for kind in ("scene", "light", "camera", "cast", "vibe", "palette", "mood"):
        for spec in CATALOG[kind]:
            CreativeOption.objects.get_or_create(
                kind=kind, key=spec["key"], defaults={k: v for k, v in spec.items() if k != "key"}
            )


def test_a_clip_tells_the_model_the_chosen_scene_light_and_cast(
    auth_client: Any, pro: Any, still: Any, looks: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai.services import rephrase

    monkeypatch.setattr(rephrase, "get_text_provider", lambda *_a, **_k: None)
    creative = {
        "scene": "garden-terrace",
        "light": "golden-hour",
        "cast": "hands",
        "moods": ["warm"],
    }
    body = auth_client.post(
        RENDERS, confirmed(auth_client, clip(still, creative=creative)), format="json"
    ).json()

    render = VideoRender.objects.get(pk=body["id"])
    sent = _fake_video_provider.calls[-1]
    assert render.spec["creative"] == creative
    for kind, key in (("scene", "garden-terrace"), ("light", "golden-hour"), ("cast", "hands")):
        assert CreativeOption.objects.get(kind=kind, key=key).prompt_fragment in sent
    assert CreativeOption.objects.get(kind="motion", key="push").prompt_fragment in sent


def test_a_reel_keeps_the_chosen_look_on_its_spec(
    auth_client: Any, pro: Any, stills: list[Any], looks: None
) -> None:
    creative = {"scene": "seamless-studio", "palette": "mediterranean"}
    body = auth_client.post(
        RENDERS, confirmed(auth_client, reel(stills, creative=creative)), format="json"
    ).json()

    assert VideoRender.objects.get(pk=body["id"]).spec["creative"] == creative


def test_a_look_is_part_of_the_price_identity(auth_client: Any, still: Any, looks: None) -> None:
    plain = auth_client.post(ESTIMATE, clip(still), format="json").json()
    styled = auth_client.post(
        ESTIMATE, clip(still, creative={"scene": "garden-terrace"}), format="json"
    ).json()

    assert plain["credits"] == styled["credits"]  # a look is free; the lengths price a video


@pytest.mark.parametrize(
    "creative",
    [
        {"scene": "atlantis"},
        {"moods": ["nope"]},
        {"format": "feed-square"},
        {"toggles": {"hashtags": True}},
    ],
)
def test_an_unknown_or_non_visual_choice_is_a_400(
    auth_client: Any, still: Any, looks: None, creative: dict[str, Any]
) -> None:
    response = auth_client.post(ESTIMATE, clip(still, creative=creative), format="json")

    assert response.status_code == 400
    assert "creative" in str(response.json()["error"])


# --- the real video adapter behind the same flow (mocked transport, no network) ----
def test_a_clip_renders_through_openrouter_on_the_packages_route(
    auth_client: Any, pro: Any, still: Any, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Route → adapter → length read from the file → one charge, with the
    model that rendered it on the row. Text stays on its fake: only the video
    port is real here, and its transport is mocked."""
    import json
    import struct

    import httpx

    from ai.models import AIModelRoute, AITask
    from ai.providers import openrouter_video
    from tests.httpx_transport import patch_httpx_transport

    settings.USE_FAKE_AI_PROVIDERS = False
    settings.USE_FAKE_TEXT_PROVIDER = True
    settings.VIDEO_PROVIDER_API_KEY = "or-test-key"
    monkeypatch.setattr(openrouter_video, "_sleep", lambda _s: None)
    AIModelRoute.objects.create(
        task=AITask.VIDEO_START,
        provider="openrouter",
        model="x-ai/grok-imagine-video-1.5-lite",
        params={"resolution": "720p"},
    )
    mvhd_body = b"\x00" * 12 + struct.pack(">II", 1000, 5000) + b"\x00" * 80
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    mp4 = (
        struct.pack(">I4s", 16, b"ftyp")
        + b"isom\x00\x00\x02\x00"
        + struct.pack(">I4s", 8 + len(mvhd), b"moov")
        + mvhd
    )
    submitted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"id": "job_9", "status": "pending"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=mp4, headers={"content-type": "video/mp4"})
        return httpx.Response(200, json={"id": "job_9", "status": "completed"})

    patch_httpx_transport(monkeypatch, handler)
    before = balance(pro)

    body = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json").json()

    assert body["status"] == "SUCCEEDED", body
    assert submitted[0]["model"] == "x-ai/grok-imagine-video-1.5-lite"
    assert submitted[0]["resolution"] == "720p"
    assert submitted[0]["frame_images"][0]["frame_type"] == "first_frame"
    render = VideoRender.objects.get(pk=body["id"])
    assert (render.provider, render.model) == ("openrouter", "x-ai/grok-imagine-video-1.5-lite")
    assert balance(pro) == before - body["credits"]


# --- the free allowance (Spark: 4 clips, lifetime, no credits) ---------------------------
@pytest.fixture
def spark(workspace: Any, catalogue: None) -> Any:
    """A plan without `video_generation` but with a free allowance, as Spark is
    seeded. No credits granted on top: a free clip must not spend any."""
    plan = workspace.organization.plan
    plan.features = {
        **plan.features,
        "video_generation": False,
        "free_video_clips": 4,
        "free_video_max_seconds": 5,
    }
    plan.save()
    return workspace


@pytest.fixture
def spark_still(spark: Any, make_png_upload: Any) -> Any:
    return ingest_media(workspace=spark, upload=make_png_upload("bottle.png", (600, 750)))


def test_a_free_clip_is_quoted_at_zero_and_rendered_without_credits(
    auth_client: Any, spark: Any, spark_still: Any
) -> None:
    before = balance(spark)
    estimate = auth_client.post(ESTIMATE, clip(spark_still), format="json")
    assert estimate.status_code == 200, estimate.json()
    assert estimate.json()["credits"] == 0

    response = auth_client.post(RENDERS, confirmed(auth_client, clip(spark_still)), format="json")

    assert response.status_code == 202, response.json()
    body = response.json()
    assert body["status"] == "SUCCEEDED" and body["credits"] == 0
    render = VideoRender.objects.get(pk=body["id"])
    assert render.free is True
    assert balance(spark) == before


def test_the_free_allowance_is_a_hard_limit_of_four_per_organization(
    auth_client: Any, spark: Any, spark_still: Any, user: Any
) -> None:
    for _ in range(4):
        body = confirmed(auth_client, clip(spark_still, text=f"take {_}"))
        assert auth_client.post(RENDERS, body, format="json").status_code == 202

    estimate = auth_client.post(ESTIMATE, clip(spark_still), format="json")
    assert estimate.status_code == 402
    error = estimate.json()["error"]
    assert error["code"] == "feature_not_available"
    assert error["upgrade"]
    assert error["detail"]["free_video_clips"] == 4

    # Pooled at the organization (L-1): a second brand has no fresh four.
    other = provision_extra_workspace(user=user, name="Second brand")
    assert other.organization_id == spark.organization_id
    with pytest.raises(FeatureNotAvailable):
        video.require_video(other, "clip", seconds=5)


def test_a_failed_free_clip_does_not_use_up_the_allowance(
    auth_client: Any, spark: Any, spark_still: Any, monkeypatch: Any
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ProviderError("down")

    monkeypatch.setattr(_fake_video_provider, "animate", broken)
    response = auth_client.post(RENDERS, confirmed(auth_client, clip(spark_still)), format="json")
    assert response.json()["status"] == "FAILED"

    assert video.free_clips_left(spark) == 4


def test_a_free_clip_longer_than_the_allowance_hears_402(
    auth_client: Any, spark: Any, spark_still: Any
) -> None:
    response = auth_client.post(ESTIMATE, clip(spark_still, length="clip-10"), format="json")

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "feature_not_available"


def test_the_free_allowance_does_not_cover_reels(
    auth_client: Any, spark: Any, make_png_upload: Any
) -> None:
    shots = [
        ingest_media(workspace=spark, upload=make_png_upload(f"s{i}.png", (600, 600 + i)))
        for i in range(4)
    ]

    response = auth_client.post(ESTIMATE, reel(shots), format="json")

    assert response.status_code == 402


def test_a_paid_plan_never_draws_on_the_free_allowance(
    auth_client: Any, pro: Any, still: Any
) -> None:
    response = auth_client.post(RENDERS, confirmed(auth_client, clip(still)), format="json")

    render = VideoRender.objects.get(pk=response.json()["id"])
    assert render.free is False and render.credits == 25


def test_another_organizations_free_clips_never_count_here(
    auth_client: Any, spark: Any, spark_still: Any, other_user: Any, make_png_upload: Any
) -> None:
    """The allowance is pooled per organization (L-1), never across tenants."""
    other = provision_workspace(other_user, name="Rival")
    other.organization.plan = spark.organization.plan
    other.organization.save(update_fields=["plan"])
    asset = ingest_media(workspace=other, upload=make_png_upload("rival.png"))
    for _ in range(4):
        VideoRender.objects.create(
            workspace=other,
            created_by=other_user,
            mode="clip",
            spec={},
            source=asset,
            estimate_id="est_x",
            credits=0,
            free=True,
            status=VideoRenderStatus.SUCCEEDED,
        )

    assert video.free_clips_left(other) == 0
    assert video.free_clips_left(spark) == 4
    response = auth_client.post(ESTIMATE, clip(spark_still), format="json")
    assert response.status_code == 200 and response.json()["credits"] == 0


def test_another_organizations_still_is_a_404_on_the_free_path(
    auth_client: Any, spark: Any, other_user: Any, make_png_upload: Any
) -> None:
    other = provision_workspace(other_user, name="Rival")
    foreign = ingest_media(workspace=other, upload=make_png_upload("rival.png"))

    response = auth_client.post(ESTIMATE, clip(foreign), format="json")

    assert response.status_code == 404


def test_a_sibling_brands_still_is_a_404_on_the_free_path(
    auth_client: Any, spark: Any, user: Any, make_png_upload: Any
) -> None:
    sibling = provision_extra_workspace(user=user, name="Sibling brand")
    foreign = ingest_media(workspace=sibling, upload=make_png_upload("sibling.png"))

    response = auth_client.post(
        ESTIMATE, clip(foreign), format="json", HTTP_X_WORKSPACE_ID=str(spark.pk)
    )

    assert response.status_code == 404
