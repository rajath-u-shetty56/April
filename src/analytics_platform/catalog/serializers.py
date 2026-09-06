from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from analytics_platform.catalog.models import Project, Workspace


def save_validated(instance):
    try:
        instance.full_clean()
        instance.save()
    except DjangoValidationError as error:
        raise serializers.ValidationError(error.message_dict) from error
    return instance


class WorkspaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workspace
        fields = ("id", "key", "name", "is_active")

    def create(self, validated_data):
        return save_validated(Workspace(**validated_data))


class ProjectSerializer(serializers.ModelSerializer):
    workspace_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Project
        fields = ("id", "workspace_id", "key", "name", "is_active")
        validators = []

    def create(self, validated_data):
        return save_validated(Project(**validated_data))
