"""Labels, saved views and preferred timetables (P3-10, P3-11).

The three things a planning surface needs that are not the posts themselves:
a way to mark them, a way to come back to a filter, and a way to say when this
brand posts.

**A timetable is stored as local wall time plus an IANA zone, never as UTC.**
That is the whole of P3-11 and it is not a preference: "we post at 9am" is a
statement about the office clock, so when the clock shifts for daylight saving
the slot moves with it. Stored as UTC, a 9am slot becomes 8am — or 10am — on
the last Sunday in March, and nobody edited anything.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from typing import Any

import pytest
from rest_framework.exceptions import ValidationError

from planning.models import Label, SavedView, Timetable

pytestmark = pytest.mark.django_db

LABELS_URL = "/api/v1/labels/"
VIEWS_URL = "/api/v1/saved-views/"
TIMETABLES_URL = "/api/v1/timetables/"


class TestLabels:
    def test_a_label_carries_a_colour(self, workspace: Any) -> None:
        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")

        assert label.colour == "#FF5722"

    def test_a_label_name_is_unique_per_workspace(self, workspace: Any) -> None:
        from django.db import IntegrityError

        Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")

        with pytest.raises(IntegrityError):
            Label.objects.create(workspace=workspace, name="Launch", colour="#000000")

    def test_two_workspaces_may_use_the_same_label_name(
        self, workspace: Any, other_user: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")

        assert Label.objects.create(workspace=theirs, name="Launch", colour="#FF5722")

    def test_a_colour_that_is_not_a_hex_triple_is_refused(self, workspace: Any) -> None:
        from django.core.exceptions import ValidationError as DjangoValidationError

        with pytest.raises(DjangoValidationError):
            Label(workspace=workspace, name="Bad", colour="red").full_clean()

    def test_the_plan_caps_how_many_labels_exist(self, workspace: Any, user: Any) -> None:
        from common.exceptions import QuotaExceeded
        from planning.services.labels import create_label

        plan = workspace.organization.plan
        plan.max_labels = 1
        plan.save(update_fields=["max_labels"])

        create_label(workspace=workspace, name="One", colour="#111111")
        with pytest.raises(QuotaExceeded):
            create_label(workspace=workspace, name="Two", colour="#222222")

    def test_labelling_a_post(self, workspace: Any, user: Any) -> None:
        from content.services.posts import create_post
        from planning.services.labels import apply_labels

        post = create_post(workspace=workspace, author=user, master_body="One")
        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")

        apply_labels(post, [label])

        assert list(post.labels.all()) == [label]

    def test_another_workspaces_label_may_not_be_applied(
        self, workspace: Any, other_user: Any, user: Any
    ) -> None:
        from content.services.posts import create_post
        from planning.services.labels import apply_labels
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        label = Label.objects.create(workspace=theirs, name="Theirs", colour="#FF5722")
        post = create_post(workspace=workspace, author=user, master_body="Mine")

        with pytest.raises(ValidationError):
            apply_labels(post, [label])

    def test_a_label_in_use_is_removed_from_its_posts_when_deleted(
        self, workspace: Any, user: Any
    ) -> None:
        # Cascade, not protect: a label is an organising device, and refusing
        # to delete one because it is used would make tidying up impossible.
        from content.services.posts import create_post
        from planning.services.labels import apply_labels

        post = create_post(workspace=workspace, author=user, master_body="One")
        label = Label.objects.create(workspace=workspace, name="Launch", colour="#FF5722")
        apply_labels(post, [label])

        label.delete()

        assert post.labels.count() == 0


class TestSavedViews:
    def test_a_saved_view_stores_its_filter(self, workspace: Any, user: Any) -> None:
        view = SavedView.objects.create(
            workspace=workspace, name="Awaiting me", filters={"status": ["PENDING_REVIEW"]}
        )

        assert view.filters["status"] == ["PENDING_REVIEW"]

    def test_an_unknown_filter_key_is_refused(self, workspace: Any) -> None:
        # A saved view whose filter nothing applies would silently show
        # everything, which is the most dangerous possible failure for a view
        # called "Awaiting me".
        from planning.services.views import validate_filters

        with pytest.raises(ValidationError):
            validate_filters({"wharever": ["x"]})

    def test_a_known_filter_key_is_accepted(self) -> None:
        from planning.services.views import validate_filters

        assert validate_filters({"status": ["DRAFT"], "content_kind": ["DOC"]})

    def test_an_unknown_status_value_is_refused(self) -> None:
        from planning.services.views import validate_filters

        with pytest.raises(ValidationError):
            validate_filters({"status": ["NOPE"]})

    def test_the_plan_caps_how_many_views_are_saved(self, workspace: Any) -> None:
        from common.exceptions import QuotaExceeded
        from planning.services.views import create_saved_view

        plan = workspace.organization.plan
        plan.included_views = 1
        plan.save(update_fields=["included_views"])

        create_saved_view(workspace=workspace, name="One", filters={})
        with pytest.raises(QuotaExceeded):
            create_saved_view(workspace=workspace, name="Two", filters={})


class TestTimetables:
    def test_a_slot_is_stored_as_local_wall_time(self, workspace: Any) -> None:
        timetable = Timetable.objects.create(
            workspace=workspace,
            name="Weekdays",
            timezone="Europe/Paris",
            slots=[{"weekday": 0, "time": "09:00"}],
        )

        assert timetable.slots[0]["time"] == "09:00"

    def test_an_unknown_timezone_is_refused(self, workspace: Any) -> None:
        from planning.services.timetables import validate_timetable

        with pytest.raises(ValidationError):
            validate_timetable([{"weekday": 0, "time": "09:00"}], timezone_name="Mars/Olympus")

    def test_a_bad_weekday_is_refused(self) -> None:
        from planning.services.timetables import validate_timetable

        with pytest.raises(ValidationError):
            validate_timetable([{"weekday": 9, "time": "09:00"}], timezone_name="Europe/Paris")

    def test_a_bad_time_is_refused(self) -> None:
        from planning.services.timetables import validate_timetable

        with pytest.raises(ValidationError):
            validate_timetable([{"weekday": 0, "time": "25:00"}], timezone_name="Europe/Paris")

    def test_a_slot_resolves_to_utc_at_the_moment_it_is_used(self, workspace: Any) -> None:
        from planning.services.timetables import resolve_slot

        # 1 March: Paris is UTC+1, so 09:00 local is 08:00Z.
        resolved = resolve_slot(dt.date(2026, 3, 1), time_str="09:00", timezone_name="Europe/Paris")

        assert resolved.hour == 8
        assert resolved.tzinfo is not None

    def test_the_same_slot_survives_a_daylight_saving_shift(self, workspace: Any) -> None:
        """**The whole point of P3-11.**

        Paris moves to UTC+2 on 29 March 2026. A 09:00 slot stored as wall time
        stays 09:00 to the office and shifts to 07:00Z underneath. Stored as
        UTC it would have stayed 08:00Z and become 10:00 to the office — an
        hour late, every day, with nobody having edited anything.
        """
        from planning.services.timetables import resolve_slot

        before = resolve_slot(dt.date(2026, 3, 1), time_str="09:00", timezone_name="Europe/Paris")
        after = resolve_slot(dt.date(2026, 4, 1), time_str="09:00", timezone_name="Europe/Paris")

        assert before.hour == 8
        assert after.hour == 7
        # The fact that matters: to the office it is 09:00 on both days. The
        # UTC instant moved precisely so the local one would not.
        paris = zoneinfo.ZoneInfo("Europe/Paris")
        assert before.astimezone(paris).hour == after.astimezone(paris).hour == 9

    def test_a_nonexistent_local_time_is_refused_rather_than_guessed(self) -> None:
        """02:30 on a spring-forward Sunday does not exist in Paris.

        Guessing costs more than refusing: whichever way it is nudged, the post
        goes out at a time nobody chose, on one day a year, and the bug reads
        as "the scheduler is unreliable".
        """
        from planning.services.timetables import resolve_slot

        with pytest.raises(ValidationError):
            resolve_slot(dt.date(2026, 3, 29), time_str="02:30", timezone_name="Europe/Paris")


class TestApi:
    def test_creating_a_label(self, auth_client: Any, workspace: Any) -> None:
        response = auth_client.post(
            LABELS_URL, {"name": "Launch", "colour": "#FF5722"}, format="json"
        )

        assert response.status_code == 201, response.json()

    def test_another_workspaces_label_is_404(
        self, auth_client: Any, other_user: Any, workspace: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        label = Label.objects.create(workspace=theirs, name="Theirs", colour="#FF5722")

        assert auth_client.get(f"{LABELS_URL}{label.id}/").status_code == 404

    def test_creating_a_saved_view(self, auth_client: Any, workspace: Any) -> None:
        response = auth_client.post(
            VIEWS_URL,
            {"name": "Awaiting", "filters": {"status": ["PENDING_REVIEW"]}},
            format="json",
        )

        assert response.status_code == 201, response.json()

    def test_a_saved_view_with_an_unknown_filter_is_a_400(
        self, auth_client: Any, workspace: Any
    ) -> None:
        response = auth_client.post(
            VIEWS_URL, {"name": "Broken", "filters": {"nope": ["x"]}}, format="json"
        )

        assert response.status_code == 400

    def test_creating_a_timetable(self, auth_client: Any, workspace: Any) -> None:
        response = auth_client.post(
            TIMETABLES_URL,
            {
                "name": "Weekdays",
                "timezone": "Europe/Paris",
                "slots": [{"weekday": 0, "time": "09:00"}],
            },
            format="json",
        )

        assert response.status_code == 201, response.json()

    def test_a_timetable_in_an_unknown_zone_is_a_400(
        self, auth_client: Any, workspace: Any
    ) -> None:
        response = auth_client.post(
            TIMETABLES_URL,
            {"name": "Nowhere", "timezone": "Mars/Olympus", "slots": []},
            format="json",
        )

        assert response.status_code == 400

    def test_another_workspaces_timetable_is_404(
        self, auth_client: Any, other_user: Any, workspace: Any
    ) -> None:
        from workspaces.services.provisioning import provision_workspace

        theirs = provision_workspace(other_user, name="Rival Studio")
        timetable = Timetable.objects.create(
            workspace=theirs, name="Theirs", timezone="UTC", slots=[]
        )

        assert auth_client.get(f"{TIMETABLES_URL}{timetable.id}/").status_code == 404
