"""Workspace scoping (design.md §11, A9).

The full sweep — every registered ViewSet 404s across workspaces — is Phase 4's
`test_cross_workspace_access_returns_404_on_every_viewset`. These cover the
mixin itself, which that sweep depends on.
"""

from __future__ import annotations

import pytest
from rest_framework.exceptions import NotAuthenticated

from common.mixins import WorkspaceScopedQuerySetMixin
from common.tests.models import FakeWorkspace, ScopedThing

pytestmark = pytest.mark.django_db


class _Request:
    def __init__(self, workspace=None):
        self.workspace = workspace


class _BaseView:
    queryset = ScopedThing.objects.all()

    def get_queryset(self):
        return self.queryset


class ScopedView(WorkspaceScopedQuerySetMixin, _BaseView):
    def __init__(self, request):
        self.request = request


@pytest.fixture
def workspaces():
    mine = FakeWorkspace.objects.create(name="mine")
    theirs = FakeWorkspace.objects.create(name="theirs")
    ScopedThing.objects.create(workspace=mine, label="mine-a")
    ScopedThing.objects.create(workspace=mine, label="mine-b")
    ScopedThing.objects.create(workspace=theirs, label="theirs-a")
    return mine, theirs


def test_queryset_is_filtered_to_the_active_workspace(workspaces) -> None:
    mine, _ = workspaces
    labels = set(ScopedView(_Request(mine)).get_queryset().values_list("label", flat=True))
    assert labels == {"mine-a", "mine-b"}


def test_other_workspaces_are_invisible(workspaces) -> None:
    """Not merely forbidden — absent, so a 404 is what the view can produce."""
    mine, theirs = workspaces
    other = ScopedThing.objects.get(workspace=theirs)

    assert not ScopedView(_Request(mine)).get_queryset().filter(pk=other.pk).exists()


def test_missing_workspace_is_refused_rather_than_returning_everything(workspaces) -> None:
    """The dangerous failure mode: an unscoped request silently seeing all rows."""
    with pytest.raises(NotAuthenticated):
        ScopedView(_Request(None)).get_queryset()


def test_workspace_field_can_be_a_traversal(workspaces) -> None:
    mine, _ = workspaces

    class NestedView(ScopedView):
        workspace_field = "workspace__id__in"

    view = NestedView(_Request(None))
    view.request = _Request([mine.id])
    assert view.get_queryset().count() == 2
