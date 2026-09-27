import pytest
from django.db import IntegrityError, transaction

from analytics_platform.catalog.models import Project, Workspace
from analytics_platform.event_catalog.models import EventDefinition, EventDefinitionStatus


@pytest.mark.django_db
def test_event_name_is_unique_within_a_project_product(project):
    EventDefinition.objects.create(
        project=project,
        product_key="contact_center",
        name="call_completed",
        owner="contact-center",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        EventDefinition.objects.create(
            project=project,
            product_key="contact_center",
            name="call_completed",
            owner="another-team",
        )


@pytest.mark.django_db
def test_same_event_name_is_available_to_different_products(project):
    EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="report_created",
    )

    definition = EventDefinition.objects.create(
        project=project,
        product_key="bi",
        name="report_created",
    )

    assert definition.product_key == "bi"


@pytest.mark.django_db
def test_same_event_name_is_available_in_another_project(project):
    EventDefinition.objects.create(
        project=project,
        name="call_completed",
        owner="contact-center",
    )
    other_workspace = Workspace.objects.create(key="other", name="Other")
    other_project = Project.objects.create(
        workspace=other_workspace,
        key="main",
        name="Main",
    )

    definition = EventDefinition.objects.create(
        project=other_project,
        product_key="contact_center",
        name="call_completed",
        owner="contact-center",
    )

    assert definition.project == other_project


@pytest.mark.django_db
def test_event_definition_is_visible_by_default(project):
    definition = EventDefinition.objects.create(
        project=project,
        product_key="contact_center",
        name="call_completed",
        owner="contact-center",
    )

    assert definition.status == EventDefinitionStatus.VISIBLE
    assert definition.last_seen_at is None
    assert str(definition) == f"{project}:contact_center/call_completed"
