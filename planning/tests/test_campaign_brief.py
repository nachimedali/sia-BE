"""Campaign brief → plan → seeded drafts (steps-plan S5).

What this file holds the step to:

- **The brief is a number and a window**, plus who, what, proof and what the
  team can do. Proof is picked from what the workspace can actually show —
  Brand Core claims with evidence on file, and medians of its own measured
  posts — or typed by the user and labelled as theirs.
- **The plan never predicts.** Every number in it comes from the brief or from
  measured data, and a model reply that adds one is replaced by the template
  wording, never shown with a caveat.
- **It is priced first and runs in the background.** The estimate is what is
  confirmed; a changed price is a 409; credits are taken once, on success.
- **Seeding only makes drafts.** Nothing is scheduled, nothing leaves draft
  without an approved decision, and seeding twice makes nothing new.
- **Progress is measured or it is a gap.** No posts, no number — never a zero.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from django.utils import timezone

from ai.models import CreativeOption
from ai.plan_seed import PLAN_CATALOG
from ai.providers.base import TextGenerationResult, TextVariant
from analytics.models import Availability, MetricSource, PostMetric
from billing.models import CreditLedger, FeatureFlag
from billing.services import ledger
from billing.services.flags import CAMPAIGN_BRIEF_S5
from common.exceptions import StateConflict
from content.models import Platform, PostStatus, PostTarget
from content.services.posts import create_post
from learn.services.narrate import numerals_are_grounded, reads_as_a_prediction
from planning.models import Campaign, CampaignItem, CampaignPlan, CampaignStatus, PlanStatus
from planning.services import plans as plan_service
from products.models import Product
from workspaces.services.provisioning import provision_extra_workspace, provision_workspace

pytestmark = pytest.mark.django_db

URL = "/api/v1/campaigns/"


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def prices(db: None) -> None:
    for kind, rows in PLAN_CATALOG.items():
        for position, spec in enumerate(rows):
            CreativeOption.objects.create(kind=kind, sort_order=position, **spec)


@pytest.fixture
def funded(paid_workspace: Any, prices: None) -> Any:
    ledger.grant_credits(paid_workspace, 100)
    return paid_workspace


def _campaign(workspace: Any, *, offset_days: int = 1, length_days: int = 21, **extra: Any) -> Any:
    start = timezone.now() + dt.timedelta(days=offset_days)
    fields: dict[str, Any] = {
        "name": "Autumn harvest",
        "starts_at": start,
        "ends_at": start + dt.timedelta(days=length_days),
        **extra,
    }
    return Campaign.objects.create(workspace=workspace, **fields)


def _brief(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "goal": {"metric": "clicks", "target": 500},
        "audience": "Home cooks in Tunis who care where their food comes from.",
        "segments": ["Home cooks", "Diaspora"],
        "offer": "Free delivery over 80 TND until the end of the campaign",
        "proof_keys": [],
        "own_proof": ["Featured in the La Presse food column, 2025"],
        "constraints": {
            "team": 2,
            "hours_per_week": 6,
            "budget": 400,
            "posts_per_week": 4,
            "channels": ["instagram", "facebook"],
        },
    }
    body.update(overrides)
    return body


def _put(client: Any, campaign: Any, body: dict[str, Any], **headers: Any) -> Any:
    return client.put(f"{URL}{campaign.pk}/brief/", body, format="json", **headers)


def _ready(client: Any, campaign: Any) -> None:
    response = _put(client, campaign, _brief())
    assert response.status_code == 200, response.json()


def _estimate(client: Any, campaign: Any) -> Any:
    return client.post(f"{URL}{campaign.pk}/plan/estimate/", {}, format="json")


def _start(client: Any, campaign: Any) -> Any:
    estimate = _estimate(client, campaign)
    assert estimate.status_code == 200, estimate.json()
    return client.post(
        f"{URL}{campaign.pk}/plan/",
        {"estimate_id": estimate.json()["estimate_id"]},
        format="json",
    )


def _published_target(workspace: Any, user: Any, platform: str = Platform.INSTAGRAM) -> Any:
    post = create_post(workspace=workspace, author=user, master_body="Pressed this morning.")
    post.status = PostStatus.PUBLISHED
    post.save(update_fields=["status"])
    return PostTarget.objects.create(post=post, platform=platform)


def _metric(target: Any, *, when: Any = None, **values: Any) -> PostMetric:
    defaults: dict[str, Any] = {
        "impressions": 100,
        "likes": 10,
        "comments": 2,
        "shares": 1,
        "clicks": 5,
        "saves": 3,
        "availability": Availability.MEASURED,
        "source": MetricSource.PROVIDER,
        "provider_key": "zernio",
    }
    return PostMetric.objects.create(
        post_target=target, captured_at=when or timezone.now(), **{**defaults, **values}
    )


class _Scripted:
    """A text provider answering with one fixed reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def generate(self, *, system: str, prompt: str, n: int, model: str | None = None) -> Any:
        self.calls.append({"system": system, "prompt": prompt})
        return TextGenerationResult(
            variants=[TextVariant(body=self.reply)],
            provider="scripted",
            model="scripted-1",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(reply: str) -> _Scripted:
        scripted = _Scripted(reply)
        monkeypatch.setattr(plan_service, "get_text_provider", lambda: scripted)
        return scripted

    return install


def _strings(value: Any) -> list[str]:
    """Every human-readable string in a plan payload."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() if key not in _STRUCTURED for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


#: Structured fields — dates, keys and platform codes — rather than prose.
_STRUCTURED = {
    "at",
    "key",
    "platform",
    "metric",
    "message",
    "carries",
    "evidence",
    "format",
    "formats",
}


# -----------------------------------------------------------------------------
# The brief
# -----------------------------------------------------------------------------
class TestBrief:
    def test_a_new_campaign_has_an_empty_brief_that_says_what_is_missing(
        self, auth_client: Any, workspace: Any
    ) -> None:
        campaign = _campaign(workspace)

        body = auth_client.get(f"{URL}{campaign.pk}/brief/").json()

        assert body["audience"] == "" and body["proof_keys"] == []
        assert body["goal"] is None
        assert set(body["missing"]) == {"goal", "audience", "offer", "proof", "channels"}
        assert body["ready"] is False

    def test_saving_a_complete_brief_makes_it_ready(self, auth_client: Any, workspace: Any) -> None:
        campaign = _campaign(workspace)

        response = _put(auth_client, campaign, _brief())

        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["ready"] is True and body["missing"] == []
        assert body["goal"] == {"metric": "clicks", "target": 500}
        assert body["constraints"]["channels"] == ["instagram", "facebook"]
        campaign.refresh_from_db()
        assert campaign.goals == [{"metric": "clicks", "target": 500}]

    def test_a_partial_save_keeps_what_it_did_not_name(
        self, auth_client: Any, workspace: Any
    ) -> None:
        campaign = _campaign(workspace)
        _put(auth_client, campaign, _brief())

        response = _put(auth_client, campaign, {"offer": "Two for one"})

        assert response.json()["offer"] == "Two for one"
        assert response.json()["audience"].startswith("Home cooks")

    def test_the_window_moves_the_campaign(self, auth_client: Any, workspace: Any) -> None:
        campaign = _campaign(workspace)
        start = timezone.now() + dt.timedelta(days=3)

        _put(
            auth_client,
            campaign,
            {"starts_at": start.isoformat(), "ends_at": (start + dt.timedelta(days=9)).isoformat()},
        )

        campaign.refresh_from_db()
        assert (campaign.ends_at - campaign.starts_at).days == 9

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"goal": {"metric": "happiness", "target": 5}}, "goal"),
            ({"goal": {"metric": "clicks", "target": -1}}, "goal"),
            ({"proof_keys": ["claim:999:organic"]}, "proof_keys"),
            ({"constraints": {"channels": ["myspace"], "posts_per_week": 3}}, "constraints"),
            ({"constraints": {"posts_per_week": -2, "channels": ["instagram"]}}, "constraints"),
            ({"constraints": {"posts_per_week": 99, "channels": ["instagram"]}}, "constraints"),
            ({"own_proof": ["x" * 301]}, "own_proof"),
        ],
    )
    def test_a_bad_field_is_400_and_saves_nothing(
        self, auth_client: Any, workspace: Any, change: dict[str, Any], field: str
    ) -> None:
        campaign = _campaign(workspace)

        response = _put(auth_client, campaign, _brief(**change))

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
        assert field in json.dumps(response.json()["error"])
        assert auth_client.get(f"{URL}{campaign.pk}/brief/").json()["audience"] == ""

    def test_a_window_that_ends_before_it_starts_is_400(
        self, auth_client: Any, workspace: Any
    ) -> None:
        campaign = _campaign(workspace)
        start = timezone.now()

        response = _put(
            auth_client,
            campaign,
            {"starts_at": start.isoformat(), "ends_at": (start - dt.timedelta(days=1)).isoformat()},
        )

        assert response.status_code == 400

    def test_a_closed_campaigns_brief_cannot_change(self, auth_client: Any, workspace: Any) -> None:
        campaign = _campaign(workspace, status=CampaignStatus.CLOSED)

        response = _put(auth_client, campaign, _brief())

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "campaign_closed"

    def test_a_viewer_reads_the_brief_but_cannot_write_it(
        self, client_as: Any, viewer_user: Any, advanced_workspace: Any
    ) -> None:
        campaign = _campaign(advanced_workspace)
        viewer = client_as(viewer_user)

        assert viewer.get(f"{URL}{campaign.pk}/brief/").status_code == 200
        assert _put(viewer, campaign, _brief()).status_code == 403


class TestProof:
    def test_a_claim_with_evidence_on_file_is_offered_as_brand_core(
        self, auth_client: Any, workspace: Any, make_png_upload: Any
    ) -> None:
        from content.services.media import ingest_media

        evidence = ingest_media(workspace=workspace, upload=make_png_upload("cert.png"))
        CreativeOption.objects.create(kind="claim", key="organic", label="Certified organic")
        Product.objects.create(
            workspace=workspace,
            name="Olive oil",
            claims=[
                {"key": "organic", "proof_media": evidence.pk},
                {"key": "best", "proof_media": None},
            ],
        )

        options = auth_client.get(f"{URL}{_campaign(workspace).pk}/brief/").json()["proof_options"]

        core = [row for row in options if row["kind"] == "core"]
        assert [row["text"] for row in core] == ["Certified organic"]
        assert "Olive oil" in core[0]["source"]

    def test_a_measured_median_needs_three_measured_posts(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        campaign = _campaign(workspace)
        for impressions in (100, 300):
            _metric(_published_target(workspace, user), impressions=impressions)

        def measured() -> list[dict[str, Any]]:
            body = auth_client.get(f"{URL}{campaign.pk}/brief/").json()
            return [row for row in body["proof_options"] if row["kind"] == "measured"]

        assert measured() == []

        _metric(_published_target(workspace, user), impressions=200)
        rows = {row["key"]: row for row in measured()}

        assert "measured:instagram:impressions" in rows
        assert "200" in rows["measured:instagram:impressions"]["text"]
        assert "3 measured posts" in rows["measured:instagram:impressions"]["source"]

    def test_a_fake_adapter_row_is_never_offered_as_proof(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        for _ in range(3):
            _metric(_published_target(workspace, user), source=MetricSource.FAKE)

        body = auth_client.get(f"{URL}{_campaign(workspace).pk}/brief/").json()

        assert [row for row in body["proof_options"] if row["kind"] == "measured"] == []


# -----------------------------------------------------------------------------
# The plan
# -----------------------------------------------------------------------------
class TestEstimate:
    def test_the_estimate_is_the_price_rows(self, auth_client: Any, funded: Any) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)

        body = _estimate(auth_client, campaign).json()

        assert body["credits"] == 8
        assert [line["credits"] for line in body["lines"]] == [2, 3, 3]
        assert body["available"] == 100
        assert body["estimate_id"]

    def test_an_edited_price_row_changes_the_estimate(self, auth_client: Any, funded: Any) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        CreativeOption.objects.filter(kind="plan_price", key="messages").update(
            metadata={"credits": 7}
        )

        assert _estimate(auth_client, campaign).json()["credits"] == 12

    def test_an_incomplete_brief_cannot_be_priced(self, auth_client: Any, funded: Any) -> None:
        campaign = _campaign(funded)

        response = _estimate(auth_client, campaign)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "brief_incomplete"
        assert "goal" in response.json()["error"]["detail"]["missing"]

    def test_a_price_row_without_credits_is_a_configuration_error_not_free(
        self, auth_client: Any, funded: Any
    ) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        CreativeOption.objects.filter(kind="plan_price", key="brief").update(metadata={})

        response = _estimate(auth_client, campaign)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "plan_price_not_configured"


class TestGeneration:
    def test_a_confirmed_plan_runs_and_charges_once(self, auth_client: Any, funded: Any) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)

        response = _start(auth_client, campaign)

        assert response.status_code == 202, response.json()
        plan = CampaignPlan.objects.get(campaign=campaign)
        assert plan.status == PlanStatus.SUCCEEDED and plan.version == 1
        assert CreditLedger.objects.filter(workspace=funded, delta=-8).count() == 1
        assert plan.charge is not None

    def test_the_plan_has_three_messages_a_channel_plan_metrics_and_a_dated_calendar(
        self, auth_client: Any, funded: Any
    ) -> None:
        campaign = _campaign(funded, length_days=14)
        _ready(auth_client, campaign)
        _start(auth_client, campaign)

        plan = auth_client.get(f"{URL}{campaign.pk}/plan/").json()["plan"]

        assert len(plan["messages"]) == 3
        assert all(message["evidence"] for message in plan["messages"])
        assert [row["platform"] for row in plan["channels"]] == ["instagram", "facebook"]
        assert sum(row["posts_per_week"] for row in plan["channels"]) == 4
        assert plan["metrics"][0]["role"] == "goal" and plan["metrics"][0]["target"] == 500
        slots = plan["calendar"]
        assert len(slots) == 8  # four a week, two weeks
        at = [dt.datetime.fromisoformat(slot["at"]) for slot in slots]
        assert at == sorted(at)
        assert campaign.starts_at <= at[0] and at[-1] <= campaign.ends_at
        assert {slot["platform"] for slot in slots} == {"instagram", "facebook"}

    def test_the_plan_contains_no_projected_outcome_and_no_invented_number(
        self, auth_client: Any, funded: Any, user: Any
    ) -> None:
        """The S5 bar: every number from the brief or from measured data."""
        for impressions in (120, 340, 210):
            _metric(_published_target(funded, user), impressions=impressions)
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        _start(auth_client, campaign)
        row = CampaignPlan.objects.get(campaign=campaign)

        prose = _strings(row.plan)

        assert prose, "the plan has prose to check"
        assert not [text for text in prose if reads_as_a_prediction(text)]
        assert [t for t in prose if not numerals_are_grounded(t, row.inputs)] == []

    def test_a_model_reply_that_predicts_is_replaced_by_the_template(
        self, auth_client: Any, funded: Any, provider: Any
    ) -> None:
        provider(
            json.dumps(
                {
                    "messages": [
                        {"title": "Fresh oil", "angle": "This will increase reach by +20%."},
                        {"title": "One grove", "angle": "Name the place."},
                        {"title": "Free delivery", "angle": "Say it plainly."},
                    ],
                    "channels": {},
                }
            )
        )
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        _start(auth_client, campaign)

        row = CampaignPlan.objects.get(campaign=campaign)

        assert row.written_by == "template"
        assert "20%" not in json.dumps(row.plan)

    def test_a_model_reply_that_invents_a_number_is_replaced_by_the_template(
        self, auth_client: Any, funded: Any, provider: Any
    ) -> None:
        provider(
            json.dumps(
                {
                    "messages": [
                        {"title": "Loved by 9000 cooks", "angle": "Lead with the crowd."},
                        {"title": "One grove", "angle": "Name the place."},
                        {"title": "Free delivery", "angle": "Say it plainly."},
                    ],
                    "channels": {},
                }
            )
        )
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        _start(auth_client, campaign)

        assert CampaignPlan.objects.get(campaign=campaign).written_by == "template"

    def test_a_clean_model_reply_is_used(
        self, auth_client: Any, funded: Any, provider: Any
    ) -> None:
        scripted = provider(
            json.dumps(
                {
                    "messages": [
                        {"title": "Pressed the day it was picked", "angle": "Show the hours."},
                        {"title": "One grove near Midoun", "angle": "Name the place."},
                        {"title": "Free delivery over 80 TND", "angle": "Say it plainly."},
                    ],
                    "channels": {"instagram": {"role": "Main stage: the story and the proof"}},
                }
            )
        )
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        _start(auth_client, campaign)

        row = CampaignPlan.objects.get(campaign=campaign)

        assert row.written_by == "model"
        assert row.plan["messages"][2]["title"] == "Free delivery over 80 TND"
        assert row.plan["channels"][0]["role"] == "Main stage: the story and the proof"
        assert "80 TND" in scripted.calls[0]["prompt"]

    def test_a_changed_price_is_409_with_the_new_estimate(
        self, auth_client: Any, funded: Any
    ) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        stale = _estimate(auth_client, campaign).json()["estimate_id"]
        CreativeOption.objects.filter(kind="plan_price", key="brief").update(
            metadata={"credits": 4}
        )

        response = auth_client.post(
            f"{URL}{campaign.pk}/plan/", {"estimate_id": stale}, format="json"
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "estimate_changed"
        assert response.json()["error"]["detail"]["estimate"]["credits"] == 10
        assert not CampaignPlan.objects.filter(campaign=campaign).exists()

    def test_without_the_credits_it_is_402_and_nothing_runs(
        self, auth_client: Any, paid_workspace: Any, prices: None
    ) -> None:
        campaign = _campaign(paid_workspace)
        _ready(auth_client, campaign)
        estimate = _estimate(auth_client, campaign).json()

        response = auth_client.post(
            f"{URL}{campaign.pk}/plan/", {"estimate_id": estimate["estimate_id"]}, format="json"
        )

        assert response.status_code == 402
        assert response.json()["error"]["code"] == "insufficient_credits"
        assert not CampaignPlan.objects.filter(campaign=campaign).exists()

    def test_a_plan_in_flight_holds_its_credits_and_blocks_a_second(
        self, auth_client: Any, funded: Any
    ) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        CampaignPlan.objects.create(
            campaign=campaign, version=1, estimate_id="est_x", credits=95, status=PlanStatus.QUEUED
        )

        assert plan_service.held_credits(funded) == 95
        response = _start(auth_client, campaign)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "plan_in_progress"

    def test_a_retried_task_neither_writes_nor_charges_twice(
        self, auth_client: Any, funded: Any
    ) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        _start(auth_client, campaign)
        row = CampaignPlan.objects.get(campaign=campaign)

        plan_service.run_plan(row.pk)

        assert CreditLedger.objects.filter(workspace=funded, delta=-8).count() == 1

    def test_a_regeneration_is_a_new_version_and_a_new_charge(
        self, auth_client: Any, funded: Any
    ) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        _start(auth_client, campaign)
        _start(auth_client, campaign)

        assert list(
            CampaignPlan.objects.filter(campaign=campaign).values_list("version", flat=True)
        ) == [2, 1]
        assert auth_client.get(f"{URL}{campaign.pk}/plan/").json()["version"] == 2
        assert CreditLedger.objects.filter(workspace=funded, delta=-8).count() == 2

    def test_a_closed_campaign_is_not_planned(self, auth_client: Any, funded: Any) -> None:
        campaign = _campaign(funded)
        _ready(auth_client, campaign)
        Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.CLOSED)

        response = auth_client.post(
            f"{URL}{campaign.pk}/plan/", {"estimate_id": "est_any"}, format="json"
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "campaign_closed"

    def test_no_plan_yet_is_204(self, auth_client: Any, workspace: Any) -> None:
        assert auth_client.get(f"{URL}{_campaign(workspace).pk}/plan/").status_code == 204

    def test_a_viewer_cannot_spend_credits_on_a_plan(
        self, client_as: Any, viewer_user: Any, advanced_workspace: Any, prices: None
    ) -> None:
        campaign = _campaign(advanced_workspace)

        response = client_as(viewer_user).post(
            f"{URL}{campaign.pk}/plan/estimate/", {}, format="json"
        )

        assert response.status_code == 403


# -----------------------------------------------------------------------------
# Seeding
# -----------------------------------------------------------------------------
def _planned(client: Any, workspace: Any, **extra: Any) -> Any:
    campaign = _campaign(workspace, length_days=14, **extra)
    _ready(client, campaign)
    assert _start(client, campaign).status_code == 202
    return campaign


def _seed(client: Any, campaign: Any) -> Any:
    return client.post(f"{URL}{campaign.pk}/seed/", {}, format="json")


class TestSeeding:
    def test_seeding_makes_one_draft_per_slot_and_schedules_nothing(
        self, auth_client: Any, funded: Any
    ) -> None:
        campaign = _planned(auth_client, funded)

        response = _seed(auth_client, campaign)

        assert response.status_code == 200, response.json()
        assert response.json()["created"] == 8 and response.json()["skipped"] == 0
        items = CampaignItem.objects.filter(campaign=campaign).select_related("post")
        assert items.count() == 8
        for item in items:
            assert item.post.status == PostStatus.DRAFT
            assert item.post.scheduled_at is None and item.post.delivery_mode == ""
            assert item.post.master_body
            assert item.plan_slot.startswith("v1:")

    def test_seeding_twice_makes_nothing_new(self, auth_client: Any, funded: Any) -> None:
        campaign = _planned(auth_client, funded)
        _seed(auth_client, campaign)

        again = _seed(auth_client, campaign)

        assert again.json() == {"created": 0, "skipped": 8}
        assert CampaignItem.objects.filter(campaign=campaign).count() == 8

    def test_the_plan_read_shows_each_slots_draft(self, auth_client: Any, funded: Any) -> None:
        campaign = _planned(auth_client, funded)
        _seed(auth_client, campaign)

        slots = auth_client.get(f"{URL}{campaign.pk}/plan/").json()["plan"]["calendar"]

        assert all(slot["draft"] and slot["draft"]["status"] == "DRAFT" for slot in slots)

    def test_a_seeded_draft_cannot_be_scheduled_without_approval(
        self, auth_client: Any, advanced_workspace: Any, prices: None
    ) -> None:
        """The S5 gate: none leaves draft without an approved decision."""
        from scheduling.services import schedule_post

        ledger.grant_credits(advanced_workspace, 100)
        campaign = _planned(auth_client, advanced_workspace)
        _seed(auth_client, campaign)
        item = CampaignItem.objects.filter(campaign=campaign).first()
        assert item is not None
        draft = item.post

        with pytest.raises(StateConflict):
            schedule_post(
                post=draft,
                delivery_mode="AUTO_PUBLISH",
                scheduled_at=timezone.now() + dt.timedelta(days=2),
                actor=advanced_workspace.organization.owner,
            )

        draft.refresh_from_db()
        assert draft.status == PostStatus.DRAFT

    def test_seeding_without_a_plan_is_409(self, auth_client: Any, workspace: Any) -> None:
        response = _seed(auth_client, _campaign(workspace))

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "plan_missing"

    def test_a_closed_campaign_is_not_seeded(self, auth_client: Any, funded: Any) -> None:
        campaign = _planned(auth_client, funded)
        Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.CLOSED)

        response = _seed(auth_client, campaign)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "campaign_closed"


# -----------------------------------------------------------------------------
# Progress and the list
# -----------------------------------------------------------------------------
class TestProgress:
    def test_nothing_measured_is_not_enough_data_never_zero(
        self, auth_client: Any, workspace: Any
    ) -> None:
        campaign = _campaign(workspace, offset_days=-3, goals=[{"metric": "clicks", "target": 50}])

        body = auth_client.get(f"{URL}{campaign.pk}/progress/").json()

        assert body["state"] == "not_enough_data"
        assert body["actual"] is None
        assert all(day["value"] is None for day in body["days"])

    def test_before_it_starts_nothing_is_measured(self, auth_client: Any, workspace: Any) -> None:
        campaign = _campaign(workspace, offset_days=4, goals=[{"metric": "clicks", "target": 50}])

        assert auth_client.get(f"{URL}{campaign.pk}/progress/").json()["state"] == "not_started"

    def test_measured_posts_count_and_unmeasured_days_are_gaps(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        midnight = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        campaign = _campaign(workspace, goals=[{"metric": "clicks", "target": 50}])
        campaign.starts_at = midnight - dt.timedelta(days=5)
        campaign.save(update_fields=["starts_at"])
        target = _published_target(workspace, user)
        CampaignItem.objects.create(campaign=campaign, post=target.post)
        _metric(target, when=campaign.starts_at + dt.timedelta(hours=6), clicks=4)
        _metric(target, when=campaign.starts_at + dt.timedelta(days=3, hours=6), clicks=10)
        fake = _published_target(workspace, user)
        CampaignItem.objects.create(campaign=campaign, post=fake.post)
        _metric(fake, source=MetricSource.FAKE, clicks=900)

        body = auth_client.get(f"{URL}{campaign.pk}/progress/").json()

        assert body["state"] == "measured"
        assert body["actual"] == 10 and body["target"] == 50
        values = [day["value"] for day in body["days"]]
        assert values[0] == 4 and values[3] == 6
        assert values[1] is None and values[2] is None  # gaps, not zeros
        assert body["gap_days"] >= 2

    def test_a_campaign_without_a_goal_says_so(self, auth_client: Any, workspace: Any) -> None:
        campaign = _campaign(workspace, offset_days=-2)

        assert auth_client.get(f"{URL}{campaign.pk}/progress/").json()["state"] == "no_goal"


class TestList:
    def test_each_row_carries_its_progress(self, auth_client: Any, workspace: Any) -> None:
        _campaign(workspace, offset_days=-2, goals=[{"metric": "clicks", "target": 50}])

        row = auth_client.get(URL).json()["results"][0]

        assert row["progress"]["state"] == "not_enough_data"
        assert row["progress"]["actual"] is None
        assert row["has_plan"] is False

    def test_filtered_and_searched_on_the_server(self, auth_client: Any, workspace: Any) -> None:
        _campaign(workspace, name="Kilim restock", status=CampaignStatus.ACTIVE)
        _campaign(workspace, name="Spring rugs", status=CampaignStatus.CLOSED)
        _campaign(workspace, name="Ramadan boxes", goals=[{"metric": "saves", "target": 9}])

        def names(query: str) -> list[str]:
            return sorted(
                row["name"] for row in auth_client.get(f"{URL}?{query}").json()["results"]
            )

        assert names("status=ACTIVE") == ["Kilim restock"]
        assert names("search=rug") == ["Spring rugs"]
        assert names("metric=saves") == ["Ramadan boxes"]
        assert auth_client.get(f"{URL}?page_size=2").json()["count"] == 3
        assert len(auth_client.get(f"{URL}?page_size=2").json()["results"]) == 2

    def test_counts_per_status(self, auth_client: Any, workspace: Any) -> None:
        _campaign(workspace, status=CampaignStatus.ACTIVE)
        _campaign(workspace, status=CampaignStatus.ACTIVE)
        _campaign(workspace)

        body = auth_client.get(f"{URL}counts/").json()

        assert body == {"ALL": 3, "PLANNING": 1, "ACTIVE": 2, "CLOSED": 0}


# -----------------------------------------------------------------------------
# Tenancy and the flag
# -----------------------------------------------------------------------------
_PATHS = ("brief/", "plan/", "progress/")


class TestTenancy:
    def test_another_organizations_campaign_is_404_everywhere(
        self, auth_client: Any, workspace: Any, other_user: Any
    ) -> None:
        theirs = _campaign(provision_workspace(other_user, name="Rival Studio"))

        for path in _PATHS:
            assert auth_client.get(f"{URL}{theirs.pk}/{path}").status_code == 404, path
        assert _put(auth_client, theirs, _brief()).status_code == 404
        assert _estimate(auth_client, theirs).status_code == 404
        assert _seed(auth_client, theirs).status_code == 404

    def test_a_sibling_workspaces_campaign_is_404_everywhere(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        sibling = provision_extra_workspace(user=user, name="Second Brand")
        theirs = _campaign(sibling)
        here = {"HTTP_X_WORKSPACE_ID": str(workspace.pk)}

        for path in _PATHS:
            assert auth_client.get(f"{URL}{theirs.pk}/{path}", **here).status_code == 404, path
        assert _put(auth_client, theirs, _brief(), **here).status_code == 404
        assert auth_client.post(f"{URL}{theirs.pk}/seed/", {}, **here).status_code == 404
        assert theirs.pk not in [
            row["id"] for row in auth_client.get(URL, **here).json()["results"]
        ]


class TestFlag:
    def test_flag_off_is_the_phase_3_campaign(self, auth_client: Any, workspace: Any) -> None:
        FeatureFlag.objects.create(
            organization=workspace.organization, key=CAMPAIGN_BRIEF_S5, enabled=False
        )
        campaign = _campaign(workspace)

        for path in _PATHS:
            assert auth_client.get(f"{URL}{campaign.pk}/{path}").status_code == 404, path
        assert _put(auth_client, campaign, _brief()).status_code == 404
        assert _seed(auth_client, campaign).status_code == 404
        # The Phase 3 surfaces are untouched.
        assert auth_client.get(f"{URL}{campaign.pk}/").status_code == 200
        assert auth_client.get(URL).json()["results"][0]["progress"] is None
