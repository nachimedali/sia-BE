"""The digest as the page reads it (digest.html, SA-70).

What the page needs beyond the stored rows, asserted: the campaign's name, how
many measured posts the window held, the stored prose re-checked against the
stored statistics, and each proposal's latest verdict — so a rejection still
reads as one after a reload. Plus the tenancy boundary on both endpoints.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.utils import timezone

from learn.models import Confidence, Digest, Finding, NarrationSource
from learn.services import proposals
from taste.models import Rule, RuleKind, RuleSet

pytestmark = pytest.mark.django_db

STATS = {
    "campaign": "",
    "window_start": "2026-09-01",
    "window_end": "2026-09-28",
    "findings": [{"segment": "CAROUSEL", "sample_size": 22, "led_in": 15, "baseline_size": 18}],
}


def _digest(workspace: Any, *, narration: str, source: str = NarrationSource.MODEL) -> Digest:
    now = timezone.now()
    digest = Digest.objects.create(
        workspace=workspace,
        window_start=now - dt.timedelta(days=28),
        window_end=now,
        statistics=STATS,
        narration=narration,
        narration_source=source,
    )
    Finding.objects.create(
        digest=digest,
        segment={"dimension": "format", "value": "CAROUSEL"},
        comparison={
            "segment_mean": 0.061,
            "baseline_mean": 0.042,
            "sample_size": 22,
            "baseline_size": 18,
            "led_in": 15,
            "campaigns_observed": 2,
        },
        confidence=Confidence.STRONG,
        sample_size=22,
        baseline_size=18,
        campaigns_observed=2,
    )
    Finding.objects.create(
        digest=digest,
        segment={"dimension": "platform", "value": "tiktok"},
        comparison={},
        confidence=Confidence.INSUFFICIENT,
        sample_size=3,
        baseline_size=37,
        excluded_reason="metric unavailable on this platform",
    )
    return digest


def _rule(digest: Digest) -> Rule:
    ruleset = RuleSet.objects.create(workspace=digest.workspace, version=1, derived_from=digest)
    return Rule.objects.create(
        ruleset=ruleset,
        kind=RuleKind.FORMAT,
        payload={"dimension": "format", "prefer": "CAROUSEL", "sample_size": 22, "led_in": 15},
        provenance=digest.findings.get(confidence=Confidence.STRONG),
    )


def test_the_digest_carries_its_window_size_and_a_passing_prose_check(
    auth_client: Any, paid_workspace: Any
) -> None:
    digest = _digest(paid_workspace, narration="Carousels led in 15 of 22 posts.")

    body = auth_client.get(f"/api/v1/digests/{digest.pk}/").json()

    assert body["posts_measured"] == 40
    assert body["campaign_name"] == ""
    assert body["prose_check"] == {"numbers": 2, "grounded": True, "predictive": False}


def test_a_figure_nothing_computed_fails_the_prose_check(
    auth_client: Any, paid_workspace: Any
) -> None:
    digest = _digest(paid_workspace, narration="Carousels led in 15 of 23 posts.")

    check = auth_client.get(f"/api/v1/digests/{digest.pk}/").json()["prose_check"]

    assert check["grounded"] is False


def test_forecast_wording_is_flagged(auth_client: Any, paid_workspace: Any) -> None:
    digest = _digest(paid_workspace, narration="Carousels will increase reach.")

    assert auth_client.get(f"/api/v1/digests/{digest.pk}/").json()["prose_check"]["predictive"]


def test_an_undecided_proposal_has_no_decision(auth_client: Any, paid_workspace: Any) -> None:
    digest = _digest(paid_workspace, narration="")
    _rule(digest)

    rule = auth_client.get(f"/api/v1/digests/{digest.pk}/").json()["proposed_rules"][0]

    assert rule["decision"] is None
    assert rule["is_accepted"] is False


def test_a_rejection_reads_as_one_after_a_reload(auth_client: Any, paid_workspace: Any) -> None:
    digest = _digest(paid_workspace, narration="")
    rule = _rule(digest)

    response = auth_client.post(
        f"/api/v1/rule-proposals/{rule.pk}/reject/",
        {"reason_code": "good_but_not_now", "note": "After the holidays"},
        format="json",
    )
    assert response.status_code == 200

    reloaded = auth_client.get(f"/api/v1/digests/{digest.pk}/").json()["proposed_rules"][0]

    assert reloaded["decision"]["verdict"] == "rejected"
    assert reloaded["decision"]["reason_code"] == "good_but_not_now"
    assert reloaded["decision"]["note"] == "After the holidays"


def test_the_newest_verdict_stands(auth_client: Any, paid_workspace: Any, user: Any) -> None:
    digest = _digest(paid_workspace, narration="")
    rule = _rule(digest)
    proposals.reject(rule, actor=user)

    auth_client.post(f"/api/v1/rule-proposals/{rule.pk}/accept/", {}, format="json")
    reloaded = auth_client.get(f"/api/v1/digests/{digest.pk}/").json()["proposed_rules"][0]

    assert reloaded["decision"]["verdict"] == "accepted"
    assert reloaded["is_accepted"] is True


def test_another_tenants_digest_and_proposal_are_404(
    paid_workspace: Any, other_user: Any, plans: dict[str, Any]
) -> None:
    from rest_framework.test import APIClient

    from workspaces.services.provisioning import provision_workspace

    digest = _digest(paid_workspace, narration="")
    rule = _rule(digest)
    theirs = provision_workspace(other_user, name="Not Yours")
    theirs.organization.plan = plans["pro"]
    theirs.organization.save(update_fields=["plan"])
    stranger = APIClient()
    stranger.force_authenticate(other_user)

    assert stranger.get(f"/api/v1/digests/{digest.pk}/").status_code == 404
    assert stranger.post(f"/api/v1/rule-proposals/{rule.pk}/reject/", {}).status_code == 404


def test_another_workspace_of_the_same_org_is_404(
    auth_client: Any, user: Any, paid_workspace: Any
) -> None:
    from workspaces.services.provisioning import provision_extra_workspace

    digest = _digest(paid_workspace, narration="")
    second = provision_extra_workspace(user=user, name="Second Brand")

    response = auth_client.get(f"/api/v1/digests/{digest.pk}/", HTTP_X_WORKSPACE_ID=str(second.pk))

    assert response.status_code == 404


def test_each_finding_carries_its_ratio_and_an_excluded_one_none(
    auth_client: Any, paid_workspace: Any
) -> None:
    digest = _digest(paid_workspace, narration="")

    findings = auth_client.get(f"/api/v1/digests/{digest.pk}/").json()["findings"]
    by_value = {row["segment"]["value"]: row for row in findings}

    assert by_value["CAROUSEL"]["ratio"] == 1.45
    assert by_value["tiktok"]["ratio"] is None
