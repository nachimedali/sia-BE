from django.contrib import admin

from workspaces.models import Membership, Workspace


class MembershipInline(admin.TabularInline):  # type: ignore[type-arg]
    model = Membership
    extra = 0
    autocomplete_fields = ("user",)


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name", "slug", "owner", "plan", "onboarding_complete", "created_at")
    list_filter = ("onboarding_complete", "plan", "business_type")
    search_fields = ("name", "slug", "owner__email")
    readonly_fields = ("referral_code", "created_at", "updated_at")
    inlines = (MembershipInline,)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "workspace", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__email", "workspace__name")
