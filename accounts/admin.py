import csv
from typing import Any

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.http import HttpResponse

from accounts.models import BetaAccessRequest, User


@admin.register(User)
# django-stubs types ModelAdmin as generic, but it is not subscriptable at
# runtime — parameterising it here breaks admin autodiscovery on boot.
class UserAdmin(DjangoUserAdmin):  # type: ignore[type-arg]
    ordering = ("-created_at",)
    list_display = ("email", "is_email_verified", "is_staff", "created_at")
    list_filter = ("is_email_verified", "is_staff", "is_superuser", "is_active")
    search_fields = ("email", "first_name", "last_name")
    readonly_fields = ("created_at", "updated_at", "last_login", "date_joined")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        ("Verification", {"fields": ("is_email_verified",)}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined", "created_at", "updated_at")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)


def _cell(value: Any) -> Any:
    """Public input opened in a spreadsheet: a leading = + - @ would run as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@admin.register(BetaAccessRequest)
class BetaAccessRequestAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "email",
        "plan",
        "currency",
        "business_name",
        "phone",
        "submissions",
        "created_at",
    )
    list_filter = ("plan", "currency", "created_at")
    search_fields = ("email", "business_name", "phone", "social_url")
    readonly_fields = ("submissions", "created_at", "updated_at")
    date_hierarchy = "created_at"
    actions = ("export_csv",)

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request: Any, queryset: Any) -> HttpResponse:
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="beta-access-requests.csv"'
        writer = csv.writer(response)
        fields = [
            "email",
            "package",
            "currency",
            "business_name",
            "phone",
            "social_url",
            "submissions",
            "created_at",
        ]
        writer.writerow(fields)
        for row in queryset.select_related("plan"):
            writer.writerow(
                [
                    _cell(v)
                    for v in (
                        row.email,
                        row.plan.code,
                        row.currency,
                        row.business_name,
                        row.phone,
                        row.social_url,
                        row.submissions,
                        row.created_at.isoformat(),
                    )
                ]
            )
        return response
