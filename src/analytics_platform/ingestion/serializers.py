from rest_framework import serializers

from analytics_platform.ingestion.models import IngestionCredential


class CredentialSerializer(serializers.ModelSerializer):
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["Unknown field."] for field in sorted(unknown)}
            )
        return super().to_internal_value(data)

    class Meta:
        model = IngestionCredential
        fields = (
            "id",
            "name",
            "prefix",
            "created_at",
            "updated_at",
            "last_used_at",
            "revoked_at",
        )
        read_only_fields = (
            "id",
            "prefix",
            "created_at",
            "updated_at",
            "last_used_at",
            "revoked_at",
        )
