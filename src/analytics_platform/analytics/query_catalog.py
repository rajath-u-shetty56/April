from datetime import UTC, datetime

from analytics_platform.analytics.contracts import BoundedList, Scope, validate_limit
from analytics_platform.catalog.models import Project
from analytics_platform.event_catalog.models import (
    EventDefinition,
    EventDefinitionStatus,
    EventPropertyDefinition,
    PropertyDefinitionStatus,
)
from analytics_platform.group_analytics.models import GroupPropertyDefinition

VISIBLE_DEFINITION_STATUSES = (
    EventDefinitionStatus.VISIBLE,
    EventDefinitionStatus.VERIFIED,
)
VISIBLE_PROPERTY_STATUSES = (
    PropertyDefinitionStatus.VISIBLE,
    PropertyDefinitionStatus.VERIFIED,
)


def discover_analytics_catalog(
    project: Project,
    *,
    event_definition_limit: int = 50,
    event_property_limit: int = 100,
    group_property_limit: int = 100,
):
    event_definition_limit = validate_limit(event_definition_limit)
    event_property_limit = validate_limit(event_property_limit)
    group_property_limit = validate_limit(group_property_limit)
    definitions = EventDefinition.objects.filter(
        project=project,
        status__in=VISIBLE_DEFINITION_STATUSES,
    ).order_by("product_key", "name")
    event_properties = (
        EventPropertyDefinition.objects.filter(
            event_definition__project=project,
            event_definition__status__in=VISIBLE_DEFINITION_STATUSES,
            status__in=VISIBLE_PROPERTY_STATUSES,
        )
        .select_related("event_definition")
        .order_by("event_definition__product_key", "event_definition__name", "property_name")
    )
    group_properties = GroupPropertyDefinition.objects.filter(
        project=project,
        status__in=VISIBLE_PROPERTY_STATUSES,
    ).order_by("group_type", "property_name")
    definition_count = definitions.count()
    event_property_count = event_properties.count()
    group_property_count = group_properties.count()
    return {
        "scope": Scope(project.pk).to_dict(),
        "products": list(
            definitions.exclude(product_key="")
            .values_list("product_key", flat=True)
            .distinct()
            .order_by("product_key")
        ),
        "counts": {
            "event_definitions": definition_count,
            "event_property_definitions": event_property_count,
            "group_property_definitions": group_property_count,
        },
        "event_definitions": BoundedList.from_items(
            [
                {
                    "product": row.product_key,
                    "event": row.name,
                    "description": row.description,
                    "owner": row.owner,
                    "status": row.status,
                    "last_seen_at": _iso(row.last_seen_at),
                }
                for row in definitions[:event_definition_limit]
            ],
            limit=event_definition_limit,
            total_count=definition_count,
        ).to_dict(),
        "event_properties": BoundedList.from_items(
            [
                {
                    "product": row.event_definition.product_key,
                    "event": row.event_definition.name,
                    "property_name": row.property_name,
                    "description": row.description,
                    "status": row.status,
                    "observed_non_null_types": row.observed_non_null_types,
                    "nullable": row.nullable,
                    "has_type_conflict": row.has_type_conflict,
                    "first_seen_at": _iso(row.first_seen_at),
                    "last_seen_at": _iso(row.last_seen_at),
                }
                for row in event_properties[:event_property_limit]
            ],
            limit=event_property_limit,
            total_count=event_property_count,
        ).to_dict(),
        "group_properties": BoundedList.from_items(
            [
                {
                    "group_type": row.group_type,
                    "property_name": row.property_name,
                    "description": row.description,
                    "status": row.status,
                    "observed_non_null_types": row.observed_non_null_types,
                    "nullable": row.nullable,
                    "has_type_conflict": row.has_type_conflict,
                    "first_seen_at": _iso(row.first_seen_at),
                    "last_seen_at": _iso(row.last_seen_at),
                }
                for row in group_properties[:group_property_limit]
            ],
            limit=group_property_limit,
            total_count=group_property_count,
        ).to_dict(),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
