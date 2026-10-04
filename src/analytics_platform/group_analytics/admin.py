from django.contrib import admin

from analytics_platform.group_analytics.models import GroupProfile, GroupPropertyDefinition


@admin.register(GroupProfile)
class GroupProfileAdmin(admin.ModelAdmin):
    list_display = ("project", "group_type", "group_key", "last_seen_at", "updated_at")
    list_filter = ("project", "group_type")
    search_fields = ("group_key",)
    readonly_fields = ("id", "created_at", "updated_at", "last_seen_at")


@admin.register(GroupPropertyDefinition)
class GroupPropertyDefinitionAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "group_type",
        "property_name",
        "status",
        "observed_non_null_types",
        "nullable",
        "has_type_conflict",
        "last_seen_at",
    )
    list_filter = ("project", "group_type", "status", "nullable")
    search_fields = ("property_name", "description")
    readonly_fields = (
        "id",
        "property_name_hash",
        "observed_non_null_types",
        "nullable",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )
