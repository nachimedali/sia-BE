"""The Phase 6 endpoints (P6-04, P6-06, P6-08, P6-01).

Every emittable code, and both tenancy dimensions, as CLAUDE.md rule 9
requires. Two of them carry the weight of the phase:

**402 on an out-of-horizon window** is the ship gate (P6-G1). A client-facing
document whose range was quietly clipped looks complete and is wrong, and
nobody can tell by reading it.

**404, never 403, across tenants.** A report belonging to another organization
must not be distinguishable from one that does not exist — the same rule the
sweep enforces on the queryset, checked here through the wire.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from analytics.models import Report, ReportRun, ReportRunStatus, ReportShareLink

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client() -> Any:
    """Nobody at all — what a guest with a link, or an enumeration attempt,
    arrives as."""
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def paid_auth_client(auth_client: Any, paid_workspace: Any) -> Any:
    """`auth_client`, with its workspace on a plan that keeps 90 days.

    Pro rather than the trial plan on purpose: the horizon refusal is the ship
    gate, and a client that could never render a 30-day report would prove
    nothing about the gate except that it fires on everything.
    """
    return auth_client


@pytest.fixture
def report(paid_workspace: Any, user: Any) -> Report:
    return Report.objects.create(
        workspace=paid_workspace,
        name="Monthly performance",
        sections=[{"kind": "channel_totals"}],
        window_days=30,
        created_by=user,
    )


@pytest.fixture
def ready_run(report: Report) -> ReportRun:
    return ReportRun.objects.create(
        report=report,
        window_start=timezone.now() - dt.timedelta(days=30),
        window_end=timezone.now(),
        status=ReportRunStatus.READY,
        payload={"report": report.name, "sections": []},
    )


# -----------------------------------------------------------------------------
# Definitions
# -----------------------------------------------------------------------------
class TestReportDefinitions:
    def test_a_report_is_created_in_the_callers_workspace(
        self, paid_auth_client: Any, paid_workspace: Any
    ) -> None:
        response = paid_auth_client.post(
            reverse("report-list"),
            {"name": "Quarterly", "sections": [{"kind": "channel_totals"}]},
            format="json",
        )

        assert response.status_code == 201
        assert Report.objects.get(pk=response.data["id"]).workspace_id == paid_workspace.pk

    def test_an_unknown_section_kind_is_refused_not_ignored(self, paid_auth_client: Any) -> None:
        """A saved report carrying a section nothing renders would produce a
        document with a hole in it, every month, silently."""
        response = paid_auth_client.post(
            reverse("report-list"),
            {"name": "Broken", "sections": [{"kind": "sales_forecast"}]},
            format="json",
        )

        assert response.status_code == 400

    def test_requires_authentication(self, api_client: Any) -> None:
        assert api_client.get(reverse("report-list")).status_code == 401

    def test_another_workspaces_report_is_404(
        self, paid_auth_client: Any, report: Report, other_workspace: tuple[Any, Any, Any]
    ) -> None:
        theirs, their_user, _account = other_workspace
        stranger = Report.objects.create(workspace=theirs, name="Theirs", created_by=their_user)

        response = paid_auth_client.get(reverse("report-detail", args=[stranger.pk]))

        assert response.status_code == 404

    def test_the_list_never_shows_another_workspaces_report(
        self, paid_auth_client: Any, report: Report, other_workspace: tuple[Any, Any, Any]
    ) -> None:
        theirs, their_user, _account = other_workspace
        Report.objects.create(workspace=theirs, name="Theirs", created_by=their_user)

        response = paid_auth_client.get(reverse("report-list"))

        assert [row["name"] for row in response.data["results"]] == ["Monthly performance"]


# -----------------------------------------------------------------------------
# Rendering — the ship gate
# -----------------------------------------------------------------------------
class TestRendering:
    def test_rendering_produces_a_run_and_a_document(
        self, paid_auth_client: Any, report: Report
    ) -> None:
        response = paid_auth_client.post(
            reverse("report-render", args=[report.pk]), {}, format="json"
        )

        assert response.status_code == 201
        assert response.data["status"] == ReportRunStatus.READY
        assert response.data["document_url"]

    def test_a_window_beyond_the_plans_horizon_is_402(
        self, auth_client: Any, workspace: Any, user: Any
    ) -> None:
        """P6-G1. The trial plan keeps seven days; a 90-day window cannot be
        honestly covered, so it refuses rather than clipping."""
        report = Report.objects.create(workspace=workspace, name="Too far", created_by=user)
        ends_at = timezone.now()

        response = auth_client.post(
            reverse("report-render", args=[report.pk]),
            {
                "starts_at": (ends_at - dt.timedelta(days=90)).isoformat(),
                "ends_at": ends_at.isoformat(),
            },
            format="json",
        )

        assert response.status_code == 402
        assert response.data["error"]["code"]
        assert ReportRun.objects.filter(report=report).count() == 0

    def test_half_a_window_is_refused_rather_than_guessed(
        self, paid_auth_client: Any, report: Report
    ) -> None:
        response = paid_auth_client.post(
            reverse("report-render", args=[report.pk]),
            {"starts_at": timezone.now().isoformat()},
            format="json",
        )

        assert response.status_code == 400

    def test_a_backwards_window_is_refused(self, paid_auth_client: Any, report: Report) -> None:
        now = timezone.now()
        response = paid_auth_client.post(
            reverse("report-render", args=[report.pk]),
            {
                "starts_at": now.isoformat(),
                "ends_at": (now - dt.timedelta(days=1)).isoformat(),
            },
            format="json",
        )

        assert response.status_code == 400

    def test_rendering_another_workspaces_report_is_404(
        self, paid_auth_client: Any, other_workspace: tuple[Any, Any, Any]
    ) -> None:
        theirs, their_user, _account = other_workspace
        stranger = Report.objects.create(workspace=theirs, name="Theirs", created_by=their_user)

        response = paid_auth_client.post(
            reverse("report-render", args=[stranger.pk]), {}, format="json"
        )

        assert response.status_code == 404


# -----------------------------------------------------------------------------
# Sharing
# -----------------------------------------------------------------------------
class TestSharingEndpoints:
    def test_sharing_returns_the_url_once_and_never_stores_the_token(
        self, paid_auth_client: Any, ready_run: ReportRun
    ) -> None:
        response = paid_auth_client.post(
            reverse("report-share", args=[ready_run.pk]), {}, format="json"
        )

        assert response.status_code == 201
        raw = response.data["url"].rsplit("/", 1)[-1]
        assert ReportShareLink.objects.filter(token_hash=raw).count() == 0

        # And the list of who holds a link does not carry it either.
        listing = paid_auth_client.get(reverse("report-share", args=[ready_run.pk]))
        assert "url" not in listing.data[0]
        assert "token_hash" not in listing.data[0]

    def test_sharing_a_run_that_has_not_rendered_is_409(
        self, paid_auth_client: Any, report: Report
    ) -> None:
        """The run exists and the caller may see it; it is in the wrong state.
        No permission and no upgrade changes that, which is what 409 means."""
        pending = ReportRun.objects.create(
            report=report,
            window_start=timezone.now() - dt.timedelta(days=7),
            window_end=timezone.now(),
            status=ReportRunStatus.PENDING,
        )

        response = paid_auth_client.post(
            reverse("report-share", args=[pending.pk]), {}, format="json"
        )

        assert response.status_code == 409

    def test_another_workspaces_run_is_404(
        self, paid_auth_client: Any, other_workspace: tuple[Any, Any, Any]
    ) -> None:
        theirs, their_user, _account = other_workspace
        stranger = Report.objects.create(workspace=theirs, name="Theirs", created_by=their_user)
        their_run = ReportRun.objects.create(
            report=stranger,
            window_start=timezone.now() - dt.timedelta(days=7),
            window_end=timezone.now(),
            status=ReportRunStatus.READY,
        )

        response = paid_auth_client.post(
            reverse("report-share", args=[their_run.pk]), {}, format="json"
        )

        assert response.status_code == 404

    def test_a_guest_reads_the_report_with_no_account(
        self, paid_auth_client: Any, api_client: Any, ready_run: ReportRun
    ) -> None:
        minted = paid_auth_client.post(
            reverse("report-share", args=[ready_run.pk]), {}, format="json"
        )
        token = minted.data["url"].rsplit("/", 1)[-1]

        response = api_client.get(reverse("shared-report", args=[token]))

        assert response.status_code == 200
        assert response.data["report_name"] == "Monthly performance"
        assert response.data["payload"] == ready_run.payload

    def test_a_revoked_link_is_404_for_the_guest(
        self, paid_auth_client: Any, api_client: Any, ready_run: ReportRun
    ) -> None:
        minted = paid_auth_client.post(
            reverse("report-share", args=[ready_run.pk]), {}, format="json"
        )
        token = minted.data["url"].rsplit("/", 1)[-1]

        revoke = paid_auth_client.post(
            reverse("report-share-revoke", args=[ready_run.pk, minted.data["id"]]),
            {},
            format="json",
        )
        assert revoke.status_code == 204

        assert api_client.get(reverse("shared-report", args=[token])).status_code == 404

    def test_an_invented_token_is_404(self, api_client: Any) -> None:
        assert api_client.get(reverse("shared-report", args=["nope"])).status_code == 404


# -----------------------------------------------------------------------------
# Demographics and competitors
# -----------------------------------------------------------------------------
class TestDemographicsEndpoint:
    def test_it_returns_the_newest_row_per_dimension(
        self, paid_auth_client: Any, social_account: Any, metrics_provider: Any
    ) -> None:
        from analytics.services import ingest

        ingest.snapshot_accounts()

        response = paid_auth_client.get(reverse("analytics-demographics"))

        assert response.status_code == 200
        assert {row["dimension"] for row in response.data} == {"AGE", "COUNTRY"}
        assert all(row["availability"] == "MEASURED" for row in response.data)

    def test_an_unavailable_row_is_returned_with_a_null_breakdown(
        self, paid_auth_client: Any, social_account: Any, metrics_provider: Any
    ) -> None:
        """The surface must render "we cannot see this yet", which it cannot do
        for a row it never received."""
        from analytics.services import ingest

        metrics_provider.tiny_accounts.add(social_account.provider_account_id)
        ingest.snapshot_accounts()

        response = paid_auth_client.get(reverse("analytics-demographics"))

        assert all(row["breakdown"] is None for row in response.data)
        assert all(row["availability"] == "UNAVAILABLE" for row in response.data)

    def test_another_workspaces_demographics_are_invisible(
        self, paid_auth_client: Any, other_workspace: tuple[Any, Any, Any], metrics_provider: Any
    ) -> None:
        from analytics.services import ingest

        ingest.snapshot_accounts()

        response = paid_auth_client.get(reverse("analytics-demographics"))

        assert response.data == []

    def test_requires_authentication(self, api_client: Any) -> None:
        assert api_client.get(reverse("analytics-demographics")).status_code == 401


class TestCompetitorEndpoints:
    def test_tracking_and_listing(self, paid_auth_client: Any, paid_workspace: Any) -> None:
        from categories.models import Category

        paid_workspace.category = Category.objects.create(name="Ceramics", slug="cer")
        paid_workspace.save(update_fields=["category"])

        created = paid_auth_client.post(
            reverse("analytics-competitors"),
            {"platform": "instagram", "handle": "@Rival", "label": "Rival"},
            format="json",
        )

        assert created.status_code == 201
        assert created.data["handle"] == "rival"

        listing = paid_auth_client.get(reverse("analytics-competitors"))
        assert [row["handle"] for row in listing.data] == ["rival"]

    def test_the_cap_is_a_402_with_an_upgrade_path(
        self, paid_auth_client: Any, paid_workspace: Any
    ) -> None:
        from categories.models import Category

        paid_workspace.category = Category.objects.create(name="Ceramics", slug="cer")
        paid_workspace.save(update_fields=["category"])
        plan = paid_workspace.organization.plan
        plan.max_tracked_competitors = 0
        plan.save(update_fields=["max_tracked_competitors"])

        response = paid_auth_client.post(
            reverse("analytics-competitors"),
            {"platform": "instagram", "handle": "rival"},
            format="json",
        )

        assert response.status_code == 402

    def test_untracking_another_workspaces_competitor_is_404(
        self, paid_auth_client: Any, other_workspace: tuple[Any, Any, Any]
    ) -> None:
        from categories.models import Category
        from trends.services import competitors

        theirs, _user, _account = other_workspace
        theirs.category = Category.objects.create(name="Ceramics", slug="cer")
        theirs.save(update_fields=["category"])
        source = competitors.track(theirs, platform="instagram", handle="rival")

        response = paid_auth_client.delete(reverse("analytics-competitor-detail", args=[source.pk]))

        assert response.status_code == 404

    def test_the_comparison_needs_a_platform(self, paid_auth_client: Any) -> None:
        response = paid_auth_client.get(reverse("analytics-competitor-comparison"))
        assert response.status_code == 400

    def test_requires_authentication(self, api_client: Any) -> None:
        assert api_client.get(reverse("analytics-competitors")).status_code == 401
