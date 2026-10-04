from django.db import models

from analytics_platform.catalog.models import Project
from analytics_platform.common.models import UUIDTimeStampedModel
from analytics_platform.common.property_catalog import (
    PropertyDefinitionStatus,
    has_type_conflict,
    hash_property_name,
)

PRODUCT_KEY_MAX_LENGTH = 80


class EventDefinitionStatus(models.TextChoices):
    VISIBLE = "visible", "Visible"
    VERIFIED = "verified", "Verified"
    HIDDEN = "hidden", "Hidden"


class EventDefinition(UUIDTimeStampedModel):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="event_definitions",
    )
    product_key = models.CharField(max_length=PRODUCT_KEY_MAX_LENGTH, blank=True, default="")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.CharField(max_length=160, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=EventDefinitionStatus.choices,
        default=EventDefinitionStatus.VISIBLE,
    )

    class Meta:
        ordering = ("project_id", "product_key", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "product_key", "name"),
                name="event_catalog_event_project_product_name_unique",
            )
        ]

    def __str__(self) -> str:
        namespace = f"{self.product_key}/" if self.product_key else ""
        return f"{self.project}:{namespace}{self.name}"


class EventPropertyDefinition(UUIDTimeStampedModel):
    event_definition = models.ForeignKey(
        EventDefinition,
        on_delete=models.CASCADE,
        related_name="property_definitions",
    )
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
        ordering = ("event_definition_id", "property_name")
        constraints = [
            models.UniqueConstraint(
                fields=("event_definition", "property_name_hash"),
                name="event_prop_definition_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("event_definition", "status", "property_name_hash"),
                name="event_prop_discovery_idx",
            )
        ]

    @property
    def has_type_conflict(self) -> bool:
        return has_type_conflict(self.observed_non_null_types)

    @staticmethod
    def hash_property_name(property_name: str) -> str:
        return hash_property_name(property_name)

    def __str__(self) -> str:
        return f"{self.event_definition}:{self.property_name}"

    def save(self, *args, **kwargs):
        self.property_name_hash = self.hash_property_name(self.property_name)
        super().save(*args, **kwargs)
