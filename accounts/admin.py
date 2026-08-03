from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from accounts.models import User


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
