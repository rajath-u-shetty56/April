from django.db import models

from analytics_platform.catalog.models import Project
from analytics_platform.common.models import UUIDTimeStampedModel
from analytics_platform.common.property_catalog import (
    PropertyDefinitionStatus,
    has_type_conflict,
    hash_property_name,
)

GROUP_TYPE_MAX_LENGTH = 80
GROUP_KEY_MAX_LENGTH = 400


class GroupProfile(UUIDTimeStampedModel):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="group_profiles",
    )
    group_type = models.CharField(max_length=GROUP_TYPE_MAX_LENGTH)
    group_key = models.CharField(max_length=GROUP_KEY_MAX_LENGTH)
    properties = models.JSONField(default=dict)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("project_id", "group_type", "group_key")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "group_type", "group_key"),
                name="group_profile_project_type_key_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("project", "group_type", "last_seen_at"),
                name="group_profile_type_seen_idx",
            )
        ]

    def __str__(self):
        return f"{self.project}:{self.group_type}/{self.group_key}"


class GroupPropertyDefinition(UUIDTimeStampedModel):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="group_property_definitions",
    )
    group_type = models.CharField(max_length=GROUP_TYPE_MAX_LENGTH)
    property_name = models.TextField()
    property_name_hash = models.CharField(max_length=64, editable=False)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=PropertyDefinitionStatus.choices,
        default=PropertyDefinitionStatus.VISIBLE,
    )
    observed_non_null_types = models.JSONField(default=list)
    nullable = models.BooleanField(default=False)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("project_id", "group_type", "property_name")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "group_type", "property_name_hash"),
                name="group_prop_definition_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("project", "group_type", "status", "property_name_hash"),
                name="group_prop_discovery_idx",
            )
        ]

    @property
    def has_type_conflict(self) -> bool:
        return has_type_conflict(self.observed_non_null_types)

    @staticmethod
    def hash_property_name(property_name: str) -> str:
        return hash_property_name(property_name)

    def __str__(self):
        return f"{self.project}:{self.group_type}.{self.property_name}"

    def save(self, *args, **kwargs):
        self.property_name_hash = self.hash_property_name(self.property_name)
        super().save(*args, **kwargs)
