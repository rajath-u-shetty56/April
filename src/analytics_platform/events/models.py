import uuid

from django.db import models
from django.utils import timezone

from analytics_platform.catalog.models import Project


class EventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError("Ingested events are immutable.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise TypeError("Ingested events are immutable.")
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=False,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise TypeError("Ingested events are immutable.")


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="events", db_index=False
    )
    uuid = models.UUIDField()
    event = models.CharField(max_length=200)
    distinct_id = models.CharField(max_length=400)
    timestamp = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now, editable=False)
    groups = models.JSONField(default=dict)
    properties = models.JSONField(default=dict)

    objects = EventQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("project", "uuid"), name="events_project_uuid_unique")
        ]
        indexes = [
            models.Index(fields=("project", "timestamp"), name="events_project_time_idx"),
            models.Index(
                fields=("project", "event", "timestamp"), name="events_project_name_time_idx"
            ),
            models.Index(
                fields=("project", "distinct_id", "timestamp"), name="events_project_actor_time_idx"
            ),
        ]

    def __str__(self):
        return str(self.uuid)

    def save(self, *args, **kwargs):
        if not self._state.adding or (
            not kwargs.get("force_insert") and type(self).objects.filter(pk=self.pk).exists()
        ):
            raise TypeError("Ingested events are immutable.")
        # Force an insert even for an explicit primary key; never overwrite a row.
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)
