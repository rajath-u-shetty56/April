from datetime import UTC, datetime
from io import StringIO
from uuid import UUID, uuid4

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from analytics_platform.event_catalog.models import EventPropertyDefinition
from analytics_platform.events.models import Event
from analytics_platform.group_analytics.models import GroupPropertyDefinition

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def producer(project):
    from analytics_platform.ingestion.credentials import create_credential

    _credential, secret = create_credential(project, name="property catalog test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {secret}")
    return client


@pytest.fixture
def event():
    return {
        "uuid": str(uuid4()),
        "event": "ticket_created",
        "distinct_id": "helpdesk:agent:42",
        "timestamp": "2026-09-17T10:30:00Z",
        "groups": {"account": "acme"},
        "properties": {"product": "helpdesk"},
    }


def test_ingestion_tracks_nullability_separately_from_non_null_type(producer, event, project):
    event["properties"].update(priority=3, optional=None)
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201

    priority = EventPropertyDefinition.objects.get(
        event_definition__project=project,
        event_definition__product_key="helpdesk",
        event_definition__name="ticket_created",
        property_name="priority",
    )
    optional = EventPropertyDefinition.objects.get(
        event_definition=priority.event_definition,
        property_name="optional",
    )
    assert priority.observed_non_null_types == ["number"]
    assert priority.nullable is False
    assert priority.has_type_conflict is False
    assert optional.observed_non_null_types == []
    assert optional.nullable is True

    null_observation = {**event, "uuid": str(uuid4())}
    null_observation["properties"] = {
        **event["properties"],
        "priority": None,
    }
    assert producer.post("/api/v1/capture/", null_observation, format="json").status_code == 201

    priority.refresh_from_db()
    assert priority.observed_non_null_types == ["number"]
    assert priority.nullable is True
    assert priority.has_type_conflict is False

    string_observation = {**event, "uuid": str(uuid4())}
    string_observation["properties"] = {
        **event["properties"],
        "priority": "urgent",
    }
    assert producer.post("/api/v1/capture/", string_observation, format="json").status_code == 201

    priority.refresh_from_db()
    assert priority.observed_non_null_types == ["number", "string"]
    assert priority.has_type_conflict is True
    assert not any(
        field.name in {"sample_values", "observed_values", "examples"}
        for field in EventPropertyDefinition._meta.get_fields()
    )


def test_groupidentify_discovers_properties_by_project_and_group_type(producer, event, project):
    event.update(
        event="$groupidentify",
        groups={},
        properties={
            "$group_type": "workspace",
            "$group_key": "north",
            "$group_set": {"plan": "pro", "seats": 12, "nullable": None},
        },
    )

    response = producer.post("/api/v1/capture/", event, format="json")

    assert response.status_code == 201
    properties = {
        row.property_name: row for row in GroupPropertyDefinition.objects.filter(project=project)
    }
    assert set(properties) == {"plan", "seats", "nullable"}
    assert properties["plan"].group_type == "workspace"
    assert properties["plan"].observed_non_null_types == ["string"]
    assert properties["seats"].observed_non_null_types == ["number"]
    assert properties["nullable"].observed_non_null_types == []
    assert properties["nullable"].nullable is True
    assert project.event_definitions.count() == 0


def test_property_catalog_backfill_resumes_by_event_primary_key_and_merges_idempotently(
    project,
):
    event_ids = [
        UUID("00000000-0000-0000-0000-000000000001"),
        UUID("00000000-0000-0000-0000-000000000002"),
        UUID("00000000-0000-0000-0000-000000000003"),
    ]
    rows = [
        Event(
            id=event_ids[0],
            project=project,
            uuid=UUID("ffffffff-ffff-ffff-ffff-fffffffffff1"),
            event="ticket_created",
            distinct_id="actor:1",
            timestamp=datetime(2026, 1, 3, tzinfo=UTC),
            groups={},
            properties={"product": "helpdesk", "priority": 2},
        ),
        Event(
            id=event_ids[1],
            project=project,
            uuid=UUID("ffffffff-ffff-ffff-ffff-fffffffffff2"),
            event="ticket_created",
            distinct_id="actor:2",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            groups={},
            properties={"product": "helpdesk", "priority": None},
        ),
        Event(
            id=event_ids[2],
            project=project,
            uuid=UUID("ffffffff-ffff-ffff-ffff-fffffffffff3"),
            event="$groupidentify",
            distinct_id="system:groups",
            timestamp=datetime(2026, 1, 2, tzinfo=UTC),
            groups={},
            properties={
                "$group_type": "workspace",
                "$group_key": "north",
                "$group_set": {"plan": "pro"},
            },
        ),
    ]
    Event.objects.bulk_create(rows)

    first_run = StringIO()
    call_command(
        "backfill_property_catalog",
        project_id=str(project.pk),
        batch_size=1,
        through_event_pk=str(event_ids[2]),
        stdout=first_run,
    )
    assert f"Last processed Event primary key: {event_ids[2]}" in first_run.getvalue()
    priority = EventPropertyDefinition.objects.get(
        event_definition__project=project,
        event_definition__product_key="helpdesk",
        event_definition__name="ticket_created",
        property_name="priority",
    )
    assert priority.observed_non_null_types == ["number"]
    assert priority.nullable is True
    assert priority.first_seen_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert priority.last_seen_at == datetime(2026, 1, 3, tzinfo=UTC)
    assert GroupPropertyDefinition.objects.get(
        project=project, group_type="workspace", property_name="plan"
    ).observed_non_null_types == ["string"]

    resumed = StringIO()
    call_command(
        "backfill_property_catalog",
        project_id=str(project.pk),
        batch_size=1,
        after_event_pk=str(event_ids[0]),
        through_event_pk=str(event_ids[2]),
        stdout=resumed,
    )
    priority.refresh_from_db()
    assert priority.observed_non_null_types == ["number"]
    assert priority.nullable is True
    assert EventPropertyDefinition.objects.count() == 2
    assert set(EventPropertyDefinition.objects.values_list("property_name", flat=True)) == {
        "priority",
        "product",
    }
    assert GroupPropertyDefinition.objects.count() == 1
    assert Event.objects.count() == 3
