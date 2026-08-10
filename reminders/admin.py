from django.contrib import admin

from reminders.models import Reminder


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("post", "state", "send_at", "sent_at", "confirmed_at")
    list_filter = ("state", "channel")
    search_fields = ("post__master_body",)
    autocomplete_fields = ("post",)
    readonly_fields = ("token_hash", "created_at", "updated_at")
