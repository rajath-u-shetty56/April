import pytest
from rest_framework.test import APIClient

from analytics_platform.catalog.models import Project, Workspace
from analytics_platform.event_catalog.models import EventDefinition


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def staff_user(db, django_user_model):
    return django_user_model.objects.create_user(
        username="admin",
        password="test-password",
        is_staff=True,
    )


@pytest.fixture
def regular_user(db, django_user_model):
    return django_user_model.objects.create_user(
        username="member",
        password="test-password",
    )


@pytest.mark.django_db
def test_catalog_api_requires_authentication(api_client):
    response = api_client.get("/api/v1/workspaces/")

    assert response.status_code == 401


@pytest.mark.django_db
def test_catalog_api_requires_staff_user(api_client, regular_user):
    api_client.force_authenticate(regular_user)

    response = api_client.get("/api/v1/workspaces/")

    assert response.status_code == 403


@pytest.mark.django_db
def test_catalog_does_not_expose_products_as_a_separate_resource(
    api_client,
    staff_user,
    workspace,
):
    api_client.force_authenticate(staff_user)

    response = api_client.get(f"/api/v1/workspaces/{workspace.id}/products/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_staff_can_create_workspace_and_project(api_client, staff_user):
    api_client.force_authenticate(staff_user)
    workspace_response = api_client.post(
        "/api/v1/workspaces/",
        {"key": "acme", "name": "Acme"},
        format="json",
    )
    workspace_id = workspace_response.data["id"]
    project_response = api_client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/",
        {"key": "production", "name": "Production"},
        format="json",
    )

    assert workspace_response.status_code == 201
    assert project_response.status_code == 201
    assert Project.objects.count() == 1


@pytest.mark.django_db
def test_staff_can_create_event_definition(api_client, staff_user, project):
    api_client.force_authenticate(staff_user)

    response = api_client.post(
        f"/api/v1/projects/{project.id}/event-definitions/",
        {
            "product_key": "contact_center",
            "name": "call_completed",
            "description": "A Contact Center call was completed",
            "owner": "contact-center",
            "status": "verified",
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["product_key"] == "contact_center"
    assert response.data["name"] == "call_completed"
    assert response.data["status"] == "verified"
    assert response.data["created_at"] is not None
    assert response.data["last_seen_at"] is None
    assert "json_schema" not in response.data
    assert "version" not in response.data


@pytest.mark.django_db
def test_event_discovery_is_scoped_to_project_and_status(
    api_client,
    staff_user,
    project,
):
    api_client.force_authenticate(staff_user)
    EventDefinition.objects.create(
        project=project,
        name="call_completed",
        owner="contact-center",
        status="verified",
    )
    EventDefinition.objects.create(
        project=project,
        name="call_started",
        owner="contact-center",
        status="hidden",
    )
    other_workspace = Workspace.objects.create(key="other", name="Other")
    other_project = Project.objects.create(
        workspace=other_workspace,
        key="main",
        name="Main",
    )
    EventDefinition.objects.create(
        project=other_project,
        name="foreign_event",
        owner="platform",
        status="verified",
    )

    response = api_client.get(
        f"/api/v1/projects/{project.id}/event-definitions/?status=verified"
    )

    assert response.status_code == 200
    assert [item["name"] for item in response.data] == ["call_completed"]


@pytest.mark.django_db
def test_event_definitions_are_only_exposed_through_a_project(
    api_client,
    staff_user,
):
    api_client.force_authenticate(staff_user)

    response = api_client.get("/api/v1/event-definitions/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_duplicate_event_name_in_same_product_returns_validation_error(
    api_client,
    staff_user,
    project,
):
    api_client.force_authenticate(staff_user)
    EventDefinition.objects.create(
        project=project,
        product_key="contact_center",
        name="call_completed",
    )

    response = api_client.post(
        f"/api/v1/projects/{project.id}/event-definitions/",
        {"product_key": "contact_center", "name": "call_completed"},
        format="json",
    )

    assert response.status_code == 400
    assert "name" in response.data


@pytest.mark.django_db
def test_same_event_name_can_be_created_for_another_product(api_client, staff_user, project):
    api_client.force_authenticate(staff_user)
    EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="report_created",
    )

    response = api_client.post(
        f"/api/v1/projects/{project.id}/event-definitions/",
        {"product_key": "bi", "name": "report_created"},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["product_key"] == "bi"


@pytest.mark.django_db
def test_staff_can_update_event_definition_metadata(api_client, staff_user, project):
    api_client.force_authenticate(staff_user)
    definition = EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="ticket_created",
    )

    response = api_client.patch(
        f"/api/v1/projects/{project.id}/event-definitions/{definition.id}/",
        {
            "description": "Created after a ticket is committed",
            "owner": "helpdesk-platform",
            "status": "verified",
        },
        format="json",
    )

    assert response.status_code == 200
    definition.refresh_from_db()
    assert definition.description == "Created after a ticket is committed"
    assert definition.owner == "helpdesk-platform"
    assert definition.status == "verified"


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["name", "product_key", "project_id", "last_seen_at"])
def test_metadata_update_rejects_identity_and_observation_fields(
    api_client, staff_user, project, field
):
    api_client.force_authenticate(staff_user)
    definition = EventDefinition.objects.create(
        project=project,
        product_key="helpdesk",
        name="ticket_created",
    )

    response = api_client.patch(
        f"/api/v1/projects/{project.id}/event-definitions/{definition.id}/",
        {field: "replacement"},
        format="json",
    )

    assert response.status_code == 400
