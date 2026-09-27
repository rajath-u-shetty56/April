from django.db import models

from analytics_platform.catalog.models import Project
from analytics_platform.common.models import UUIDTimeStampedModel

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
