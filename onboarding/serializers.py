"""Onboarding wizard (design.md §10.4, implementation.md Phase 2.4).

Eight steps (six with the brand import off), resumable: each step PATCHes the
fields it owns, and the resource reports which step the user should be on.
Resumability is the point — a wizard that loses progress on a refresh is a
wizard people abandon.

What counts as done lives in `onboarding.services.wizard`; this renders it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from onboarding.services import wizard
from workspaces.models import Workspace
from workspaces.serializers import WorkspaceProfileSerializer


class OnboardingSerializer(WorkspaceProfileSerializer):
    """The profile plus where the wizard stands. Validation is the profile's."""

    # Derived, read-only: the FE uses these to route and to render the rail.
    current_step = serializers.SerializerMethodField()
    completed_steps = serializers.SerializerMethodField()
    email_verified = serializers.SerializerMethodField()
    plan_code = serializers.SerializerMethodField()
    #: Which steps this workspace actually has to answer, and whether that is
    #: the shortened per-brand re-run (P0-58). The FE routes on these rather
    #: than assuming a count, so a second brand does not land on a plan picker it
    #: has no business seeing.
    applicable_steps = serializers.SerializerMethodField()
    is_shortened = serializers.SerializerMethodField()

    class Meta(WorkspaceProfileSerializer.Meta):
        fields = (
            *WorkspaceProfileSerializer.Meta.fields,
            "onboarding_complete",
            "current_step",
            "completed_steps",
            "email_verified",
            "plan_code",
            "applicable_steps",
            "is_shortened",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "onboarding_complete")

    # --- derived ---------------------------------------------------------
    def _user(self) -> Any:
        return self.context["request"].user

    def get_email_verified(self, obj: Workspace) -> bool:
        return bool(self._user().is_email_verified)

    def get_applicable_steps(self, workspace: Workspace) -> list[int]:
        return wizard.steps_for(workspace)

    def get_is_shortened(self, workspace: Workspace) -> bool:
        return wizard.is_shortened(workspace)

    def get_plan_code(self, obj: Workspace) -> str | None:
        plan = obj.organization.plan
        return plan.code if plan is not None else None

    def get_completed_steps(self, obj: Workspace) -> list[int]:
        return wizard.completed_steps(obj, self._user())

    def get_current_step(self, obj: Workspace) -> int:
        """The first step not yet satisfied — this is what makes it resumable."""
        return wizard.current_step(obj, self._user())
