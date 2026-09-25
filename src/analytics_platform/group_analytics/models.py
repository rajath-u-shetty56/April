from django.db import models

from analytics_platform.catalog.models import Project
from analytics_platform.common.models import UUIDTimeStampedModel

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
