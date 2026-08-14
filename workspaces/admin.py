from django.contrib import admin

from common.admin import ReadOnlyAdmin, all_fields_except_id
from workspaces.models import ApprovalAction, AuditLog, Membership, PostComment, Workspace


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


@admin.register(PostComment)
class PostCommentAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("id", "post", "author", "resolved_at", "created_at")
    list_filter = ("resolved_at",)
    search_fields = ("post__master_body", "author__email", "body")
    autocomplete_fields = ("post", "author", "parent")


@admin.register(ApprovalAction)
class ApprovalActionAdmin(ReadOnlyAdmin):
    list_display = ("id", "post", "actor", "action", "created_at")
    list_filter = ("action",)
    search_fields = ("post__master_body", "actor__email")
    readonly_fields = all_fields_except_id(ApprovalAction)


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ("id", "workspace", "actor", "verb", "created_at")
    list_filter = ("verb",)
    search_fields = ("workspace__name", "actor__email", "target_repr")
    readonly_fields = all_fields_except_id(AuditLog)
