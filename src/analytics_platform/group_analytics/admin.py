from django.contrib import admin

from analytics_platform.group_analytics.models import GroupProfile


@admin.register(GroupProfile)
class GroupProfileAdmin(admin.ModelAdmin):
    list_display = ("project", "group_type", "group_key", "last_seen_at", "updated_at")
    list_filter = ("project", "group_type")
    search_fields = ("group_key",)
    readonly_fields = ("id", "created_at", "updated_at", "last_seen_at")
