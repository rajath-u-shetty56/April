import json
from datetime import UTC

from django.conf import settings
from rest_framework import serializers

from analytics_platform.event_catalog.models import PRODUCT_KEY_MAX_LENGTH
from analytics_platform.ingestion.errors import IngestionError


class StrictStringField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str) or not data.strip():
            self.fail("invalid")
        return super().to_internal_value(data)


class EventUUIDField(serializers.UUIDField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class EventPayloadSerializer(serializers.Serializer):
    event = StrictStringField(max_length=200, trim_whitespace=False)
    distinct_id = StrictStringField(max_length=400, trim_whitespace=False)
    uuid = EventUUIDField(required=False)
    timestamp = serializers.DateTimeField(required=False, default_timezone=UTC)
    groups = serializers.JSONField(required=False, default=dict)
    properties = serializers.JSONField(required=False, default=dict)

    def validate_groups(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Groups must be an object.")
        return value

    def validate_properties(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Properties must be an object.")
        return value


def json_bytes(value):
    # PostgreSQL JSONB cannot represent NUL, non-finite numbers, or lone surrogates.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and "\x00" in item:
            raise ValueError("Unsupported JSON character")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return encoded.encode("utf-8")


def validate_event(payload):
    if not isinstance(payload, dict):
        raise IngestionError("INVALID_ENVELOPE")
    if set(payload) - set(EventPayloadSerializer().fields):
        raise IngestionError("UNKNOWN_ENVELOPE_FIELD")
    serializer = EventPayloadSerializer(data=payload)
    if not serializer.is_valid():
        field = next(iter(serializer.errors))
        raise IngestionError("INVALID_" + field.upper(), field)
    value = serializer.validated_data
    for field in ("event", "distinct_id", "groups", "properties"):
        try:
            size = len(json_bytes(value[field]))
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
            raise IngestionError("INVALID_" + field.upper(), field) from error
        if field == "properties" and size > settings.INGESTION_MAX_PROPERTY_BYTES:
            raise IngestionError("PROPERTIES_TOO_LARGE", field, 413)
    if "product" in value["properties"]:
        product_key = value["properties"]["product"]
        if (
            not isinstance(product_key, str)
            or not product_key.strip()
            or product_key != product_key.strip()
            or len(product_key) > PRODUCT_KEY_MAX_LENGTH
        ):
            raise IngestionError("INVALID_PROPERTIES", "properties.product")
    return value
