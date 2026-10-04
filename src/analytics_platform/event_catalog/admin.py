from django.contrib import admin

from analytics_platform.event_catalog.models import EventDefinition, EventPropertyDefinition


@admin.register(EventDefinition)
class EventDefinitionAdmin(admin.ModelAdmin):
    list_display = ("project", "product_key", "name", "status", "last_seen_at")
    list_filter = ("project", "product_key", "status")
    search_fields = ("name", "description", "owner")
    readonly_fields = ("id", "created_at", "updated_at", "last_seen_at")


@admin.register(EventPropertyDefinition)
class EventPropertyDefinitionAdmin(admin.ModelAdmin):
    list_display = (
        "event_definition",
        "property_name",
        "status",
        "observed_non_null_types",
        "nullable",
        "has_type_conflict",
        "last_seen_at",
    )
    list_filter = ("event_definition__project", "status", "nullable")
    search_fields = ("property_name", "description", "event_definition__name")
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
    list_select_related = ("event_definition", "event_definition__project")
