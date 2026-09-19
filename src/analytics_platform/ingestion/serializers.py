from rest_framework import serializers

from analytics_platform.ingestion.models import IngestionCredential


class CredentialSerializer(serializers.ModelSerializer):
    class Meta:
        model = IngestionCredential
        fields = (
            "id",
            "name",
            "prefix",
            "require_product",
            "require_account",
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
