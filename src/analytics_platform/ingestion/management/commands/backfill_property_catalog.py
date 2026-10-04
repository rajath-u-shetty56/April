from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from analytics_platform.catalog.models import Project
from analytics_platform.event_catalog.services import (
    observe_event_properties,
    touch_event_definition,
)
from analytics_platform.events.models import Event
from analytics_platform.group_analytics.services import observe_group_properties
from analytics_platform.ingestion.contracts import group_identify_values
from analytics_platform.ingestion.errors import IngestionError


class Command(BaseCommand):
    help = "Backfill observational event and group property catalogs in resumable batches."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", required=True)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--after-event-pk")
        parser.add_argument("--through-event-pk")

    @staticmethod
    def _event_pk(value, option_name):
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise CommandError(f"{option_name} must be an Event primary key UUID") from exc

    def handle(self, *args, **options):
        try:
            project_id = UUID(str(options["project_id"]))
            project = Project.objects.get(pk=project_id)
        except (TypeError, ValueError, Project.DoesNotExist) as exc:
            raise CommandError("project-id must identify an existing project") from exc

        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("batch-size must be a positive integer")
        after_event_pk = self._event_pk(options.get("after_event_pk"), "after-event-pk")
        through_event_pk = self._event_pk(
            options.get("through_event_pk"), "through-event-pk"
        )
        event_scope = Event.objects.filter(project_id=project.pk)
        if through_event_pk is None:
            through_event_pk = (
                event_scope.order_by("-id").values_list("id", flat=True).first()
            )
        if (
            after_event_pk is not None
            and through_event_pk is not None
            and after_event_pk > through_event_pk
        ):
            raise CommandError("after-event-pk must not be greater than through-event-pk")

        self.stdout.write(f"Through Event primary key: {through_event_pk or '<none>'}")
        processed = 0
        cursor = after_event_pk
        while through_event_pk is not None:
            batch_query = event_scope.filter(id__lte=through_event_pk)
            if cursor is not None:
                batch_query = batch_query.filter(id__gt=cursor)
            batch = list(
                batch_query.order_by("id").values("id", "event", "timestamp", "properties")[
                    :batch_size
                ]
            )
            if not batch:
                break
            with transaction.atomic():
                for row in batch:
                    self._observe_event(project, row)
            cursor = batch[-1]["id"]
            processed += len(batch)
            self.stdout.write(
                f"Processed {processed} events; Last processed Event primary key: {cursor}"
            )

        if processed == 0:
            cursor_label = str(cursor) if cursor is not None else "<none>"
            self.stdout.write(f"Last processed Event primary key: {cursor_label}")
        self.stdout.write(self.style.SUCCESS(f"Backfill complete: processed {processed} events."))

    @staticmethod
    def _observe_event(project, row):
        occurred_at = row["timestamp"]
        if row["event"] == "$groupidentify":
            try:
                group_type, _group_key, properties = group_identify_values(row)
            except IngestionError as exc:
                raise CommandError(
                    f"Stored groupidentify event {row['id']} has invalid group metadata"
                ) from exc
            observe_group_properties(
                project=project,
                group_type=group_type,
                properties=properties,
                occurred_at=occurred_at,
            )
            return

        properties = row["properties"]
        definition = touch_event_definition(
            project=project,
            product_key=properties.get("product", ""),
            name=row["event"],
            occurred_at=occurred_at,
        )
        observe_event_properties(
            definition=definition,
            properties=properties,
            occurred_at=occurred_at,
        )
