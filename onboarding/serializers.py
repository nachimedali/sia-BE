"""Onboarding wizard (design.md §10.4, implementation.md Phase 2.4).

Six steps, resumable: each step PATCHes the fields it owns, and the resource
reports which step the user should be on. Resumability is the point — a wizard
that loses progress on a refresh is a wizard people abandon.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework import serializers

from categories.models import Category
from workspaces.models import BrandVoice, BusinessType, Workspace

# design.md §10.4. What makes a step *done* — deliberately only the one thing
# each step exists to collect, not every field on it.
#
# Requiring the optional fields too (website, target audience) made pressing
# Continue advance the user while `current_step` stayed put, so resuming sent
# them backwards. Steps 1 and 5 are satisfied by email verification and plan
# assignment rather than by a workspace field, and step 6 by the flag itself.
STEP_COMPLETION_FIELDS: dict[int, tuple[str, ...]] = {
    1: (),
    # Not `name`: registration always derives a placeholder from the email, so
    # keying on it would mark the Brand step done before the user saw it.
    # `description` is the one thing here only the user can supply — and it is
    # what grounds every later generation.
    2: ("description",),
    3: ("category",),
    # timezone always carries a default, so it cannot signal engagement;
    # choosing where to post can only have come from the user.
    4: ("platforms",),
    5: (),
    6: (),
}

# What `complete/` insists on. Deliberately short: D14 says nobody hits a
# paywall or a wall of required fields before seeing the product.
REQUIRED_TO_COMPLETE: tuple[str, ...] = ("name", "category", "timezone")

TOTAL_STEPS = 6


class OnboardingSerializer(serializers.ModelSerializer[Workspace]):
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.filter(is_active=True), allow_null=True, required=False
    )
    business_type = serializers.ChoiceField(
        choices=BusinessType.choices, required=False, allow_blank=True
    )
    brand_voice_default = serializers.ChoiceField(choices=BrandVoice.choices, required=False)

    # Derived, read-only: the FE uses these to route and to render the rail.
    current_step = serializers.SerializerMethodField()
    completed_steps = serializers.SerializerMethodField()
    email_verified = serializers.SerializerMethodField()
    plan_code = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = (
            "id",
            "name",
            "website",
            "description",
            "brand_voice_default",
            "category",
            "business_type",
            "target_audience",
            "timezone",
            "regions",
            "platforms",
            "onboarding_complete",
            "current_step",
            "completed_steps",
            "email_verified",
            "plan_code",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = ("id", "onboarding_complete")

    # --- derived ---------------------------------------------------------
    def _user(self) -> Any:
        return self.context["request"].user

    def get_email_verified(self, obj: Workspace) -> bool:
        return bool(self._user().is_email_verified)

    def get_plan_code(self, obj: Workspace) -> str | None:
        return obj.plan.code if obj.plan is not None else None

    def get_completed_steps(self, obj: Workspace) -> list[int]:
        done: list[int] = []
        if self._user().is_email_verified:
            done.append(1)
        for step in (2, 3, 4):
            fields = STEP_COMPLETION_FIELDS[step]
            if fields and all(self._filled(obj, name) for name in fields):
                done.append(step)
        if obj.plan_id:
            done.append(5)
        if obj.onboarding_complete:
            done.append(6)
        return done

    def get_current_step(self, obj: Workspace) -> int:
        """The first step not yet satisfied — this is what makes it resumable."""
        completed = set(self.get_completed_steps(obj))
        for step in range(1, TOTAL_STEPS + 1):
            if step not in completed:
                return step
        return TOTAL_STEPS

    @staticmethod
    def _filled(obj: Workspace, field: str) -> bool:
        value = getattr(obj, f"{field}_id", None) if field == "category" else getattr(obj, field)
        if isinstance(value, (list, dict)):
            return len(value) > 0
        return value not in (None, "")

    # --- validation ------------------------------------------------------
    def validate_timezone(self, value: str) -> str:
        import zoneinfo

        if value and value not in zoneinfo.available_timezones():
            raise serializers.ValidationError(f"'{value}' is not a recognised IANA timezone.")
        return value

    def validate_regions(self, value: Any) -> list[str]:
        return self._string_list("regions", value)

    def validate_platforms(self, value: Any) -> list[str]:
        return self._string_list("platforms", value)

    @staticmethod
    def _string_list(name: str, value: Any) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise serializers.ValidationError(f"{name} must be a list of strings.")
        # Deduplicate while preserving order, so a double-tapped chip in the
        # wizard does not persist twice.
        seen: dict[str, None] = {}
        for item in value:
            cleaned = item.strip()
            if cleaned:
                seen.setdefault(cleaned, None)
        return list(seen)

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Business name cannot be blank.")
        return value.strip()


class OnboardingCompleteSerializer(serializers.Serializer[Any]):
    """No input. `complete/` validates the workspace it already has."""
