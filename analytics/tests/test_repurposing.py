"""Repurposing (design.md §8.9).

Two of the six named tests live here: `test_qualifying_post_enters_repurpose_
queue` and `test_repurpose_similarity_capped_at_point_eight`.
"""

from __future__ import annotations

from typing import Any

import pytest

from analytics.models import RepurposeCandidate, RepurposeConfig, RepurposeReason
from analytics.services import repurposing
from analytics.tests.conftest import make_population

pytestmark = pytest.mark.django_db

PRO_HORIZON = 90


def _population(workspace: Any, user: Any, account: Any, *, age_days: int = 75) -> Any:
    """75 days because the eligible band is genuinely narrow: §8.9 requires
    `published_at < now-60d`, and the percentile it ranks on is computed over the
    trailing 90 days, so only posts aged 60-90 days can ever qualify."""
    return make_population(
        workspace, user, account, age_days=age_days, winner_kept_earning=True
    )


# -----------------------------------------------------------------------------
# test_qualifying_post_enters_repurpose_queue
# -----------------------------------------------------------------------------
def test_qualifying_post_enters_repurpose_queue(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    winner = _population(paid_workspace, user, social_account)

    assert repurposing.scan_all() == 1

    candidate = RepurposeCandidate.objects.get()
    assert candidate.post_id == winner.post_id
    assert candidate.percentile == 100.0
    # Kept earning well past 72h, so evergreen rather than spike-and-die.
    assert candidate.reason == RepurposeReason.EVERGREEN
    # score = 100*0.6 + min(3.0*50, 100)*0.4 = 60 + 40
    assert candidate.score == pytest.approx(100.0)


def test_a_post_younger_than_sixty_days_does_not_qualify(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """§8.9's first rule. Reissuing something published last week is not
    repurposing, it is repetition."""
    _population(paid_workspace, user, social_account, age_days=30)

    assert repurposing.scan_all() == 0


def test_a_post_below_the_threshold_does_not_qualify(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    _population(paid_workspace, user, social_account)
    config = RepurposeConfig.get_solo()
    config.percentile_threshold = 100.5  # nothing can clear it
    config.save(update_fields=["percentile_threshold"])

    assert repurposing.scan_all() == 0


def test_the_threshold_is_admin_tunable(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """§8.9 calls it admin-tunable, so it is a row rather than a constant."""
    _population(paid_workspace, user, social_account)
    config = RepurposeConfig.get_solo()
    config.percentile_threshold = 50.0
    config.save(update_fields=["percentile_threshold"])

    # Lowering it lets the second-best posts through too.
    assert repurposing.scan_all() > 1


def test_a_recent_reissue_blocks_a_second_suggestion(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """§8.9's "no reissue in 90 days"."""
    winner = _population(paid_workspace, user, social_account)
    repurposing.scan_all()
    candidate = RepurposeCandidate.objects.get()
    repurposing.accept(candidate, author_id=user.pk)

    assert repurposing.scan_all() == 0
    assert not RepurposeCandidate.objects.filter(
        post_id=winner.post_id, reissued_post__isnull=True
    ).exists()


def test_re_scanning_refreshes_the_open_candidate_rather_than_stacking(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    """The scan is nightly over the same corpus, so yesterday's qualifier
    qualifies again — the queue has to stay one row per post."""
    _population(paid_workspace, user, social_account)

    repurposing.scan_all()
    repurposing.scan_all()
    repurposing.scan_all()

    assert RepurposeCandidate.objects.count() == 1


def test_a_free_workspace_is_skipped(
    workspace: Any, user: Any, social_account: Any, plans: dict[str, Any]
) -> None:
    """Repurposing is paid (§4.1), so candidates are not built for a workspace
    that could never open them."""
    workspace.plan = plans["free"]
    workspace.save(update_fields=["plan"])
    _population(workspace, user, social_account)

    assert repurposing.scan_all() == 0


def test_dismissing_takes_it_out_of_the_queue(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    _population(paid_workspace, user, social_account)
    repurposing.scan_all()

    repurposing.dismiss(RepurposeCandidate.objects.get())

    assert not RepurposeCandidate.objects.filter(dismissed_at__isnull=True).exists()


# -----------------------------------------------------------------------------
# test_repurpose_similarity_capped_at_point_eight
# -----------------------------------------------------------------------------
def test_repurpose_similarity_capped_at_point_eight(make_png_upload: Any) -> None:
    """§8.9: "accepting opens a `REPURPOSE` generation carrying `origin_post`,
    capped at 0.8 similarity to the original".

    Enforced in the quality gate, where every other similarity judgement is
    already made — a second implementation would be a second opinion about what
    "similar" means. Driven with real images and the real dHash, not a mocked
    score: a reissue that is byte-identical to the original must fail, and one
    that is genuinely different must pass.
    """
    import io

    from ai.providers.fake import FakeTextProvider, _gradient
    from ai.services import quality

    assert quality.REPURPOSE_MAX_SIMILARITY == 0.8

    original = make_png_upload(name="origin.png").read()

    # A gradient rather than another flat colour: dHash encodes the *sign* of
    # each adjacent-pixel step, so two flat images hash identically whatever
    # their colour or size. This is the same reason the image fake draws one.
    buffer = io.BytesIO()
    _gradient(600, 600, seed=7).save(buffer, format="PNG")
    different = buffer.getvalue()

    too_close = quality.run_image_quality_gate(
        content=original,
        requested_aspect="1:1",
        reference_images=[],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
        origin_images=[original],
    )
    assert too_close.passed is False
    assert "repurpose_distance" in too_close.rejected_reason

    far_enough = quality.run_image_quality_gate(
        content=different,
        requested_aspect="1:1",
        reference_images=[],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
        origin_images=[original],
    )
    assert far_enough.passed is True


def test_the_repurpose_ceiling_does_not_apply_to_ordinary_generations(
    make_png_upload: Any
) -> None:
    """Only a reissue has an original to be too close to; every other mode must
    be unaffected by the ceiling."""
    from ai.providers.fake import FakeTextProvider
    from ai.services import quality

    content = make_png_upload(name="a.png").read()

    result = quality.run_image_quality_gate(
        content=content,
        requested_aspect="1:1",
        reference_images=[content],
        restrictions=[],
        text_provider=FakeTextProvider(),
        identity_similarity_threshold=0.6,
    )

    assert result.passed is True
    assert "repurpose_distance" not in result.checks


def test_accepting_opens_a_draft_that_knows_its_origin(
    paid_workspace: Any, user: Any, social_account: Any
) -> None:
    winner = _population(paid_workspace, user, social_account)
    repurposing.scan_all()
    candidate = RepurposeCandidate.objects.get()

    reissue = repurposing.accept(candidate, author_id=user.pk)

    candidate.refresh_from_db()
    assert candidate.reissued_post_id == reissue.pk
    assert reissue.pk != winner.post_id
    assert reissue.workspace_id == paid_workspace.pk
    assert reissue.status == "DRAFT"
    # The link is what makes the 0.8 cap applicable at generation time.
    assert candidate.post.repurposed_from.first() is None
    assert reissue.repurposed_from.first() == candidate
