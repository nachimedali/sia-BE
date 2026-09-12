"""Campaigns — the theme container and the planning sprint in one (P3-04, P3-05).

**One entity for both roles, with nullable fields for what a `THEME` does not
need.** Two overlapping containers would fragment the calendar, the digest and
the rule lineage: a post would belong to a theme *and* a sprint, every view
would union two relations, and Phase 7's digest would have to choose which one
it hangs off.

**Per workspace, and they may overlap.** There is no global sprint clock
anywhere — an agency runs different brands on different cadences, and even one
brand runs an always-on theme across a two-week launch sprint.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from content.models import ContentKind
from content.services.posts import create_post
from planning.models import Campaign, CampaignItem, CampaignKind, CampaignStatus

pytestmark = pytest.mark.django_db

URL = "/api/v1/campaigns/"


def _window(offset_days: int = 0, length_days: int = 14) -> dict[str, Any]:
    start = timezone.now() + dt.timedelta(days=offset_days)
    return {"starts_at": start, "ends_at": start + dt.timedelta(days=length_days)}


def _campaign(workspace: Any, **overrides: Any) -> Campaign:
    fields: dict[str, Any] = {"name": "Spring launch", **_window(), **overrides}
    return Campaign.objects.create(workspace=workspace, **fields)


class TestShape:
    def test_a_campaign_starts_in_planning(self, workspace: Any) -> None:
        assert _campaign(workspace).status == CampaignStatus.PLANNING

    def test_a_campaign_is_a_theme_unless_someone_says_otherwise(self, workspace: Any) -> None:
        # The always-on container is the common case; a sprint is the one with
        # a deadline someone chose.
        assert _campaign(workspace).kind == CampaignKind.THEME

    def test_two_campaigns_may_overlap_in_the_same_workspace(self, workspace: Any) -> None:
        _campaign(workspace, name="Always-on brand")
        _campaign(workspace, name="Launch sprint", kind=CampaignKind.SPRINT)

        assert Campaign.objects.filter(workspace=workspace).count() == 2

    def test_a_campaign_that_ends_before_it_starts_is_refused(self, workspace: Any) -> None:
        from django.core.exceptions import ValidationError as DjangoValidationError

        now = timezone.now()
        with pytest.raises(DjangoValidationError):
            Campaign(
                workspace=workspace,
                name="Backwards",
                starts_at=now,
                ends_at=now - dt.timedelta(days=1),
            ).full_clean()

    def test_a_brief_must_be_a_document(self, workspace: Any, user: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.campaigns import set_brief

        social = create_post(workspace=workspace, author=user, master_body="Not a brief")

        with pytest.raises(ValidationError):
            set_brief(_campaign(workspace), social)

    def test_a_brief_from_another_workspace_is_refused(
        self, workspace: Any, other_user: Any
    ) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.campaigns import set_brief
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        brief = create_post(
            workspace=theirs,
            author=other_user,
            content_kind=ContentKind.DOC,
            doc_body=[{"type": "paragraph", "text": "Ours."}],
        )

        with pytest.raises(ValidationError):
            set_brief(_campaign(workspace), brief)


class TestGoals:
    def test_a_declared_goal_is_accepted(self, workspace: Any) -> None:
        from planning.services.campaigns import validate_goals

        assert validate_goals([{"metric": "reach", "target": 20000}])

    def test_an_unknown_metric_is_refused(self, workspace: Any) -> None:
        # A goal against a metric nothing measures would render as a target
        # that never moves, which reads as the product being broken.
        from rest_framework.exceptions import ValidationError

        from planning.services.campaigns import validate_goals

        with pytest.raises(ValidationError):
            validate_goals([{"metric": "vibes", "target": 10}])

    def test_a_non_numeric_target_is_refused(self, workspace: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.campaigns import validate_goals

        with pytest.raises(ValidationError):
            validate_goals([{"metric": "reach", "target": "lots"}])

    def test_a_negative_target_is_refused(self, workspace: Any) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.campaigns import validate_goals

        with pytest.raises(ValidationError):
            validate_goals([{"metric": "reach", "target": -5}])


class TestLifecycle:
    def test_planning_advances_to_active(self, workspace: Any, user: Any) -> None:
        from planning.services.campaigns import advance

        campaign = _campaign(workspace)

        assert advance(campaign, CampaignStatus.ACTIVE, actor=user).status == CampaignStatus.ACTIVE

    def test_active_advances_to_closed(self, workspace: Any, user: Any) -> None:
        from planning.services.campaigns import advance

        campaign = _campaign(workspace, status=CampaignStatus.ACTIVE)

        assert advance(campaign, CampaignStatus.CLOSED, actor=user).status == CampaignStatus.CLOSED

    def test_planning_may_not_skip_straight_to_closed(self, workspace: Any, user: Any) -> None:
        # A campaign that never ran has nothing for Phase 7's Learn to read, and
        # closing it would produce a digest over an empty window.
        from common.exceptions import StateConflict
        from planning.services.campaigns import advance

        with pytest.raises(StateConflict):
            advance(_campaign(workspace), CampaignStatus.CLOSED, actor=user)

    def test_a_closed_campaign_does_not_reopen(self, workspace: Any, user: Any) -> None:
        from common.exceptions import StateConflict
        from planning.services.campaigns import advance

        campaign = _campaign(workspace, status=CampaignStatus.CLOSED)

        with pytest.raises(StateConflict):
            advance(campaign, CampaignStatus.ACTIVE, actor=user)

    def test_closing_stamps_when_it_happened(self, workspace: Any, user: Any) -> None:
        # Phase 7 reads this to bound the window it analyses; `ends_at` is what
        # was *planned*, which is not the same fact.
        from planning.services.campaigns import advance

        campaign = _campaign(workspace, status=CampaignStatus.ACTIVE)

        assert advance(campaign, CampaignStatus.CLOSED, actor=user).closed_at is not None


class TestMembership:
    def test_a_post_joins_a_campaign(self, workspace: Any, user: Any) -> None:
        from planning.services.campaigns import add_post

        campaign = _campaign(workspace)
        post = create_post(workspace=workspace, author=user, master_body="One")

        add_post(campaign, post, actor=user)

        assert CampaignItem.objects.filter(campaign=campaign, post=post).count() == 1

    def test_adding_the_same_post_twice_is_idempotent(self, workspace: Any, user: Any) -> None:
        from planning.services.campaigns import add_post

        campaign = _campaign(workspace)
        post = create_post(workspace=workspace, author=user, master_body="One")

        add_post(campaign, post, actor=user)
        add_post(campaign, post, actor=user)

        assert CampaignItem.objects.filter(campaign=campaign, post=post).count() == 1

    def test_a_document_may_join_a_campaign(self, workspace: Any, user: Any) -> None:
        # The whole reason DOC is a subtype: campaign membership is one of the
        # seven subsystems a sibling model would have had to duplicate.
        from planning.services.campaigns import add_post

        campaign = _campaign(workspace)
        doc = create_post(
            workspace=workspace,
            author=user,
            content_kind=ContentKind.DOC,
            doc_body=[{"type": "paragraph", "text": "The brief."}],
        )

        add_post(campaign, doc, actor=user)

        assert CampaignItem.objects.filter(campaign=campaign, post=doc).exists()

    def test_a_post_may_belong_to_two_overlapping_campaigns(
        self, workspace: Any, user: Any
    ) -> None:
        from planning.services.campaigns import add_post

        theme = _campaign(workspace, name="Always-on")
        sprint = _campaign(workspace, name="Launch", kind=CampaignKind.SPRINT)
        post = create_post(workspace=workspace, author=user, master_body="One")

        add_post(theme, post, actor=user)
        add_post(sprint, post, actor=user)

        assert CampaignItem.objects.filter(post=post).count() == 2

    def test_another_workspaces_post_may_not_join(
        self, workspace: Any, other_user: Any, user: Any
    ) -> None:
        from rest_framework.exceptions import ValidationError

        from planning.services.campaigns import add_post
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        post = create_post(workspace=theirs, author=other_user, master_body="Theirs")

        with pytest.raises(ValidationError):
            add_post(_campaign(workspace), post, actor=user)

    def test_a_closed_campaign_takes_no_more_posts(self, workspace: Any, user: Any) -> None:
        from common.exceptions import StateConflict
        from planning.services.campaigns import add_post

        campaign = _campaign(workspace, status=CampaignStatus.CLOSED)
        post = create_post(workspace=workspace, author=user, master_body="Late")

        with pytest.raises(StateConflict):
            add_post(campaign, post, actor=user)


class TestQuota:
    def test_the_plan_caps_how_many_campaigns_exist(
        self, workspace: Any, user: Any, plans: dict[str, Any]
    ) -> None:
        from common.exceptions import QuotaExceeded
        from planning.services.campaigns import create_campaign

        plan = workspace.organization.plan
        plan.max_campaigns = 1
        plan.save(update_fields=["max_campaigns"])

        create_campaign(workspace=workspace, actor=user, name="First", **_window())
        with pytest.raises(QuotaExceeded):
            create_campaign(workspace=workspace, actor=user, name="Second", **_window())

    def test_a_closed_campaign_still_counts(
        self, workspace: Any, user: Any, plans: dict[str, Any]
    ) -> None:
        # It still holds its posts, its goals and — from Phase 7 — its digest.
        # Excluding it would let a workspace keep the cap at one forever by
        # closing each campaign before opening the next.
        from common.exceptions import QuotaExceeded
        from planning.services.campaigns import create_campaign

        plan = workspace.organization.plan
        plan.max_campaigns = 1
        plan.save(update_fields=["max_campaigns"])

        first = create_campaign(workspace=workspace, actor=user, name="First", **_window())
        first.status = CampaignStatus.CLOSED
        first.save(update_fields=["status"])

        with pytest.raises(QuotaExceeded):
            create_campaign(workspace=workspace, actor=user, name="Second", **_window())


class TestSeededQuotas:
    """Every plan can actually use the feature it was given.

    `max_campaigns`, `max_labels` and `included_views` were added as columns in
    Phase 0 and left at their model default of `0` until Phase 3 read them —
    which meant the quota check went live and **no plan could create a
    campaign at all**. A column added one phase ahead of its consumer is
    exactly the failure that looks like a broken feature rather than an unseeded
    number, so it is pinned here rather than trusted.
    """

    def test_every_plan_allows_at_least_one_campaign(self, plans: dict[str, Any]) -> None:
        for code, plan in plans.items():
            assert plan.max_campaigns != 0, f"{code} cannot create a campaign"

    def test_every_plan_allows_at_least_one_label(self, plans: dict[str, Any]) -> None:
        for code, plan in plans.items():
            assert plan.max_labels != 0, f"{code} cannot create a label"

    def test_every_plan_allows_at_least_one_saved_view(self, plans: dict[str, Any]) -> None:
        for code, plan in plans.items():
            assert plan.included_views != 0, f"{code} cannot save a view"


class TestApi:
    def test_creating_a_campaign(self, auth_client: Any, workspace: Any) -> None:
        response = auth_client.post(URL, {"name": "Spring", **_window()}, format="json")

        assert response.status_code == 201, response.json()
        assert response.json()["status"] == "PLANNING"

    def test_listing_shows_only_this_workspace(
        self, auth_client: Any, workspace: Any, other_user: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        _campaign(workspace, name="Mine")
        _campaign(provision_workspace(other_user, name="Rival Studio"), name="Theirs")

        response = auth_client.get(URL)

        assert [row["name"] for row in response.json()["results"]] == ["Mine"]

    def test_another_workspaces_campaign_is_404(
        self, auth_client: Any, other_user: Any, workspace: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        theirs = _campaign(provision_workspace(other_user, name="Rival Studio"))

        assert auth_client.get(f"{URL}{theirs.id}/").status_code == 404

    def test_advancing_through_the_api(self, auth_client: Any, workspace: Any) -> None:
        campaign = _campaign(workspace)

        response = auth_client.post(
            f"{URL}{campaign.id}/status/", {"status": "ACTIVE"}, format="json"
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ACTIVE"

    def test_an_illegal_transition_through_the_api_is_409(
        self, auth_client: Any, workspace: Any
    ) -> None:
        campaign = _campaign(workspace)

        response = auth_client.post(
            f"{URL}{campaign.id}/status/", {"status": "CLOSED"}, format="json"
        )

        assert response.status_code == 409

    def test_adding_a_post_through_the_api(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        campaign = _campaign(workspace)
        post = create_post(workspace=workspace, author=user, master_body="One")

        response = auth_client.post(f"{URL}{campaign.id}/items/", {"post": post.id}, format="json")

        assert response.status_code == 201, response.json()

    def test_adding_another_workspaces_post_through_the_api_is_400(
        self, auth_client: Any, workspace: Any, other_user: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        post = create_post(workspace=theirs, author=other_user, master_body="Theirs")

        response = auth_client.post(
            f"{URL}{_campaign(workspace).id}/items/", {"post": post.id}, format="json"
        )

        assert response.status_code == 400

    def test_removing_a_post(self, auth_client: Any, workspace: Any, user: Any) -> None:
        from planning.services.campaigns import add_post

        campaign = _campaign(workspace)
        post = create_post(workspace=workspace, author=user, master_body="One")
        add_post(campaign, post, actor=user)

        response = auth_client.delete(f"{URL}{campaign.id}/items/{post.id}/")

        assert response.status_code == 204
        assert not CampaignItem.objects.filter(campaign=campaign, post=post).exists()
