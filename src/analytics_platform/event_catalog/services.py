from django.db import transaction

from analytics_platform.event_catalog.models import EventDefinition


@transaction.atomic
def touch_event_definition(*, project, product_key, name, occurred_at):
    definition, _ = EventDefinition.objects.get_or_create(
        project=project,
        product_key=product_key,
        name=name,
    )
    definition = EventDefinition.objects.select_for_update().get(pk=definition.pk)
    if definition.last_seen_at is None or occurred_at > definition.last_seen_at:
        definition.last_seen_at = occurred_at
        definition.save(update_fields=("last_seen_at", "updated_at"))
    return definition
