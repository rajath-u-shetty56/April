from django.db import models


def observed_non_null_type(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    raise TypeError(f"Unsupported JSON property value: {type(value).__name__}")


def observe_properties(
    model: type[models.Model],
    scope: dict[str, object],
    properties: dict[str, object],
    occurred_at,
) -> None:
    """Merge type and timestamp observations without retaining property values."""
    for property_name, value in sorted(properties.items()):
        if not isinstance(property_name, str):
            continue
        property_type = observed_non_null_type(value)
        property_name_hash = model.hash_property_name(property_name)
        definition, _ = model.objects.get_or_create(
            **scope,
            property_name_hash=property_name_hash,
            defaults={
                "property_name": property_name,
                "observed_non_null_types": [property_type] if property_type else [],
                "nullable": value is None,
                "first_seen_at": occurred_at,
                "last_seen_at": occurred_at,
            },
        )
        if definition.property_name != property_name:
            raise RuntimeError("Property name hash collision")
        definition = model.objects.select_for_update().get(pk=definition.pk)

        observed_types = set(definition.observed_non_null_types)
        if property_type is not None:
            observed_types.add(property_type)
        merged_types = sorted(observed_types)
        first_seen_at = (
            occurred_at
            if definition.first_seen_at is None
            else min(definition.first_seen_at, occurred_at)
        )
        last_seen_at = (
            occurred_at
            if definition.last_seen_at is None
            else max(definition.last_seen_at, occurred_at)
        )
        nullable = definition.nullable or value is None
        changed_fields = []
        if merged_types != definition.observed_non_null_types:
            definition.observed_non_null_types = merged_types
            changed_fields.append("observed_non_null_types")
        if nullable != definition.nullable:
            definition.nullable = nullable
            changed_fields.append("nullable")
        if first_seen_at != definition.first_seen_at:
            definition.first_seen_at = first_seen_at
            changed_fields.append("first_seen_at")
        if last_seen_at != definition.last_seen_at:
            definition.last_seen_at = last_seen_at
            changed_fields.append("last_seen_at")
        if changed_fields:
            definition.save(update_fields=(*changed_fields, "updated_at"))
