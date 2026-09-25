from rest_framework import serializers

from analytics_platform.event_catalog.models import EventDefinition


class EventDefinitionSerializer(serializers.ModelSerializer):
    project_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = EventDefinition
        fields = (
            "id",
            "project_id",
            "product_key",
            "name",
            "description",
            "owner",
            "status",
        )

    def validate(self, attrs):
        project = self.context.get("project")
        product_key = attrs.get("product_key", "")
        name = attrs.get("name")
        if (
            project
            and name
            and project.event_definitions.filter(product_key=product_key, name=name).exists()
        ):
            raise serializers.ValidationError(
                {"name": "This event already exists for the product in the project."}
            )
        return attrs
