from rest_framework import serializers

from analytics_platform.event_catalog.models import EventDefinition


class EventDefinitionSerializer(serializers.ModelSerializer):
    project_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = EventDefinition
        fields = ("id", "project_id", "name", "description", "owner", "status")

    def validate_name(self, value):
        project = self.context.get("project")
        if project and project.event_definitions.filter(name=value).exists():
            raise serializers.ValidationError("This event already exists in the project.")
        return value
