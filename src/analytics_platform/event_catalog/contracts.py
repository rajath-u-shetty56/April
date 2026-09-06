from rest_framework import serializers


class EventPayloadSerializer(serializers.Serializer):
    """Validate the common event envelope until the ingestion app owns it."""

    event = serializers.CharField(max_length=200)
    distinct_id = serializers.CharField(max_length=400)
    timestamp = serializers.DateTimeField(required=False)
    properties = serializers.JSONField(required=False, default=dict)

    def validate_properties(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Properties must be a JSON object.")
        return value
