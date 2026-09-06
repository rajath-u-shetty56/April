from django.db import models

from analytics_platform.common.models import UUIDTimeStampedModel


class Workspace(UUIDTimeStampedModel):
    key = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("key",)

    def __str__(self) -> str:
        return self.key


class Project(UUIDTimeStampedModel):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="projects")
    key = models.SlugField(max_length=80)
    name = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("workspace__key", "key")
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "key"),
                name="catalog_project_workspace_key_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.workspace.key}/{self.key}"
