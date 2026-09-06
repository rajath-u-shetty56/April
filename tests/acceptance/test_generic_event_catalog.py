import pytest

from analytics_platform.catalog.models import Project, Workspace
from analytics_platform.event_catalog.models import EventDefinition


@pytest.mark.django_db
def test_unrelated_workspaces_use_same_event_catalog_without_platform_code_changes():
    happyfox = Workspace.objects.create(key="happyfox", name="HappyFox")
    happyfox_project = Project.objects.create(
        workspace=happyfox,
        key="main",
        name="Main",
    )
    call_event = EventDefinition.objects.create(
        project=happyfox_project,
        name="call_completed",
        description="A Contact Center call was completed",
        owner="contact-center",
    )

    example_cloud = Workspace.objects.create(key="example-cloud", name="Example Cloud")
    example_project = Project.objects.create(
        workspace=example_cloud,
        key="main",
        name="Main",
    )
    publish_event = EventDefinition.objects.create(
        project=example_project,
        name="article_published",
        description="A knowledge-base article was published",
        owner="knowledge",
    )

    assert call_event.project.workspace_id != publish_event.project.workspace_id
    assert call_event.status == publish_event.status == "visible"
