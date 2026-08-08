"""Onboarding wizard (design.md §10.4, implementation.md Phase 2.4).

Six steps, resumable: each step PATCHes the fields it owns, and the resource
reports which step the user should be on. Resumability is the point — a wizard
that loses progress on a refresh is a wizard people abandon.

What counts as done lives in `onboarding.services.wizard`; this renders it.
"""

from __future__ import annotations

import functools
import zoneinfo
from typing import Any, ClassVar

from rest_framework import serializers

from categories.models import Category
from onboarding.services import wizard
from workspaces.models import BrandVoice, BusinessType, Workspace


@functools.lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    """`available_timezones()` walks TZPATH and rebuilds its set on every call —
    ~6ms, which is not something to spend per save on the wizard's own path."""
    return frozenset(zoneinfo.available_timezones())


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
        return wizard.completed_steps(obj, self._user())

    def get_current_step(self, obj: Workspace) -> int:
        """The first step not yet satisfied — this is what makes it resumable."""
        return wizard.current_step(obj, self._user())

    # --- validation ------------------------------------------------------
    def validate_timezone(self, value: str) -> str:
        if value and value not in _known_timezones():
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
