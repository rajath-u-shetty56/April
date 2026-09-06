from django.db import models

from analytics_platform.catalog.models import Project
from analytics_platform.common.models import UUIDTimeStampedModel


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
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.CharField(max_length=160, blank=True)
    status = models.CharField(
        max_length=16,
        choices=EventDefinitionStatus.choices,
        default=EventDefinitionStatus.VISIBLE,
    )

    class Meta:
        ordering = ("project_id", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "name"),
                name="event_catalog_event_project_name_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project}:{self.name}"
