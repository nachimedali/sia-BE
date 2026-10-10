"""Seeding a taste profile from what the brand already publishes (P5-04).

**A blank profile at first run is the single largest predictor of early churn**
— the same reasoning behind `Product.is_generation_ready`. A workspace that has
to describe its own voice from nothing before the product does anything useful
will mostly not bother.

So the profile is *inferred* from the posts they already have and handed over
as a **draft the user corrects**, never as a finished answer. Everything here
is counted in code: the numbers are measured from real posts, and nothing is
guessed by a model (Part 7 rule 15's habit, applied a phase early).
"""

from __future__ import annotations

from typing import Any

import pytest

from content.models import PostStatus
from content.services.posts import create_post

pytestmark = pytest.mark.django_db


def _published(workspace: Any, user: Any, body: str) -> Any:
    post = create_post(workspace=workspace, author=user, master_body=body)
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status"])
    return post


class TestInference:
    def test_it_reads_only_published_history(self, workspace: Any, user: Any) -> None:
        """A draft is what someone was *considering*, not what the brand
        sounds like — inferring voice from abandoned drafts would teach the
        profile the opposite of what shipped."""
        from taste.services.seeding import infer_profile_fields

        _published(workspace, user, "Shipped copy #launch")
        create_post(workspace=workspace, author=user, master_body="a draft nobody ran")

        inferred = infer_profile_fields(workspace)

        assert inferred["sampled_posts"] == 1

    def test_it_measures_the_typical_hashtag_count(self, workspace: Any, user: Any) -> None:
        from taste.services.seeding import infer_profile_fields

        _published(workspace, user, "One #a #b")
        _published(workspace, user, "Two #a #b")
        _published(workspace, user, "Three #a #b #c")

        # The median, not the mean: one outlier post with thirty tags should
        # not become the brand's declared style.
        assert infer_profile_fields(workspace)["structural"]["hashtag_count"] == 2

    def test_it_notices_an_emoji_habit(self, workspace: Any, user: Any) -> None:
        from taste.services.seeding import infer_profile_fields

        _published(workspace, user, "Morning ☕")
        _published(workspace, user, "Evening 🌙")

        assert infer_profile_fields(workspace)["voice"]["emoji_policy"] == "uses_emoji"

    def test_it_notices_the_absence_of_one(self, workspace: Any, user: Any) -> None:
        from taste.services.seeding import infer_profile_fields

        _published(workspace, user, "Plain copy")
        _published(workspace, user, "More plain copy")

        assert infer_profile_fields(workspace)["voice"]["emoji_policy"] == "no_emoji"

    def test_it_says_nothing_rather_than_guessing_from_no_history(self, workspace: Any) -> None:
        """**The honest empty answer.** A workspace with no published posts has
        not shown us its voice, and inventing "friendly, 2 hashtags" would put
        a fabricated brand in front of someone who would then have to notice it
        was wrong."""
        from taste.services.seeding import infer_profile_fields

        inferred = infer_profile_fields(workspace)

        assert inferred["sampled_posts"] == 0
        assert inferred["voice"] == {}
        assert inferred["structural"] == {}

    def test_it_never_infers_hard_constraints(self, workspace: Any, user: Any) -> None:
        """Policy is not observable from output. That a brand has never
        mentioned a competitor is not evidence that it forbids it, and a
        constraint invented here would silently screen out real work."""
        from taste.services.seeding import infer_profile_fields

        _published(workspace, user, "Anything at all")

        assert "hard_constraints" not in infer_profile_fields(workspace)


class TestSeeding:
    def test_seeding_creates_an_inactive_draft_for_the_user_to_correct(
        self, workspace: Any, user: Any
    ) -> None:
        """Inferred, **then corrected** — never activated on the brand's behalf.
        A profile nobody read is one that produces content nobody recognises."""
        from taste.services.seeding import seed_profile

        _published(workspace, user, "Shipped copy #launch")

        profile = seed_profile(workspace=workspace, created_by=user)

        assert profile.is_active is False
        assert profile.version == 1

    def test_seeding_twice_does_not_stack_up_versions(self, workspace: Any, user: Any) -> None:
        # Onboarding can be resumed, and a resumed step must not mint a second
        # draft the user then has to choose between.
        from taste.models import TasteProfile
        from taste.services.seeding import seed_profile

        _published(workspace, user, "Shipped copy")

        seed_profile(workspace=workspace, created_by=user)
        seed_profile(workspace=workspace, created_by=user)

        assert TasteProfile.objects.filter(workspace=workspace).count() == 1

    def test_seeding_leaves_an_existing_profile_alone(self, workspace: Any, user: Any) -> None:
        from taste.services.profiles import activate, create_profile
        from taste.services.seeding import seed_profile

        existing = activate(create_profile(workspace=workspace, voice={"tone": "chosen"}))

        assert seed_profile(workspace=workspace, created_by=user).pk == existing.pk

    def test_the_seeded_profile_carries_what_was_measured(self, workspace: Any, user: Any) -> None:
        from taste.services.seeding import seed_profile

        _published(workspace, user, "One #a #b ☕")
        _published(workspace, user, "Two #a #b ☕")

        profile = seed_profile(workspace=workspace, created_by=user)

        assert profile.structural["hashtag_count"] == 2
        assert profile.voice["emoji_policy"] == "uses_emoji"

    def test_a_seeded_profile_scores_below_complete(self, workspace: Any, user: Any) -> None:
        """The score is what tells the user there is still something to say —
        an inferred profile that read as finished would never be corrected."""
        from taste.services.profiles import completeness
        from taste.services.seeding import seed_profile

        _published(workspace, user, "One #a ☕")

        assert completeness(seed_profile(workspace=workspace, created_by=user)) < 1.0


def _ready_to_finish(workspace: Any, user: Any) -> None:
    """The preconditions `complete_onboarding` checks, so these tests are
    about the seeding rather than about the wizard's own validation."""
    from categories.models import Category

    user.is_email_verified = True
    user.save(update_fields=["is_email_verified"])
    workspace.category = workspace.category or Category.objects.create(name="Ceramics")
    workspace.timezone = workspace.timezone or "Europe/Lisbon"
    workspace.save(update_fields=["category", "timezone"])


class TestOnboardingSeedsIt:
    def test_finishing_onboarding_leaves_a_profile_to_correct(
        self, workspace: Any, user: Any
    ) -> None:
        from onboarding.services.wizard import complete_onboarding
        from taste.models import TasteProfile

        _ready_to_finish(workspace, user)
        _published(workspace, user, "Shipped copy #launch ☕")

        complete_onboarding(workspace, user)

        profile = TasteProfile.objects.get(workspace=workspace)
        assert profile.is_active is False
        assert profile.voice["emoji_policy"] == "uses_emoji"

    def test_completing_twice_does_not_overwrite_what_the_user_wrote(
        self, workspace: Any, user: Any
    ) -> None:
        """Onboarding is resumable, and a second completion must not disturb a
        profile somebody has since corrected."""
        from onboarding.services.wizard import complete_onboarding
        from taste.models import TasteProfile

        _ready_to_finish(workspace, user)
        complete_onboarding(workspace, user)

        profile = TasteProfile.objects.get(workspace=workspace)
        profile.voice = {"tone": "corrected by hand"}
        profile.save(update_fields=["voice"])

        complete_onboarding(workspace, user)

        profile.refresh_from_db()
        assert profile.voice == {"tone": "corrected by hand"}
        assert TasteProfile.objects.filter(workspace=workspace).count() == 1
