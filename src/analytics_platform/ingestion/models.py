from django.db import models

from analytics_platform.catalog.models import Project
from analytics_platform.common.models import UUIDTimeStampedModel


class IngestionCredential(UUIDTimeStampedModel):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="ingestion_credentials"
    )
    name = models.CharField(max_length=160)
    prefix = models.CharField(max_length=32, unique=True, editable=False)
    secret_hash = models.CharField(max_length=64, editable=False)
    require_product = models.BooleanField(default=False)
    require_account = models.BooleanField(default=False)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self):
        return self.name
