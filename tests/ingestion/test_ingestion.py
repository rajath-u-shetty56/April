import json
from uuid import uuid4

import pytest
from django.apps import apps
from django.utils import timezone
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def staff(django_user_model):
    user = django_user_model.objects.create_user(username="staff", is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def credential(staff, project):
    response = staff.post(
        f"/api/v1/projects/{project.pk}/ingestion-credentials/", {"name": "Helpdesk"}, format="json"
    )
    assert response.status_code == 201
    return response.data


@pytest.fixture
def producer(credential):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {credential['secret']}")
    return client


@pytest.fixture
def event():
    return {
        "uuid": str(uuid4()),
        "event": "ticket_created",
        "distinct_id": "helpdesk:agent:42",
        "timestamp": "2026-09-17T10:30:00Z",
        "groups": {"account": "acme"},
        "properties": {
            "product": "helpdesk",
            "version": 1,
            "ticket_id": "T-100",
            "arbitrary": {"nested": [True, None, 1]},
        },
    }


def events():
    return apps.get_model("events", "Event").objects


def test_credential_secret_only_returned_once(staff, project, credential):
    model = apps.get_model("ingestion", "IngestionCredential")
    row = model.objects.get(pk=credential["id"])
    assert credential["secret"].startswith(row.prefix)
    assert row.secret_hash != credential["secret"]
    assert credential["secret"] not in str(row.__dict__)
    response = staff.get(f"/api/v1/projects/{project.pk}/ingestion-credentials/")
    assert response.status_code == 200
    assert "secret" not in response.data[0]
    assert "secret_hash" not in response.data[0]


def test_capture_persists_identity_and_flexible_properties(producer, event, project):
    before = timezone.now()
    response = producer.post("/api/v1/capture/", event, format="json")
    assert response.status_code == 201
    assert response.data["status"] == "accepted"
    assert response.data["uuid"] == event["uuid"]
    row = events().get()
    assert row.project == project
    assert row.groups == event["groups"]
    assert row.distinct_id == event["distinct_id"]
    assert row.properties == event["properties"]
    assert row.timestamp.isoformat() == "2026-09-17T10:30:00+00:00"
    assert before <= row.received_at <= timezone.now()
    definition = project.event_definitions.get()
    assert definition.name == event["event"]
    assert definition.product_key == "helpdesk"
    assert definition.status == "visible"
    credential = apps.get_model("ingestion", "IngestionCredential").objects.get()
    assert credential.last_used_at is not None


def test_retry_and_conflicting_uuid(producer, event):
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    retry = producer.post("/api/v1/capture/", event, format="json")
    assert retry.status_code == 200
    assert retry.data["duplicate"] is True
    event["properties"]["ticket_id"] = "different"
    conflict = producer.post("/api/v1/capture/", event, format="json")
    assert conflict.status_code == 409
    assert conflict.data["code"] == "UUID_CONFLICT"
    assert events().count() == 1


def test_generated_uuid_and_omitted_timestamp_retry(producer):
    payload = {"event": "job_run", "distinct_id": "system:workflows"}
    response = producer.post("/api/v1/capture/", payload, format="json")
    assert response.status_code == 201
    payload["uuid"] = response.data["uuid"]
    row = events().get()
    assert row.groups == row.properties == {}
    assert row.timestamp == row.received_at
    assert row.project.event_definitions.get().product_key == ""
    response = producer.post("/api/v1/capture/", payload, format="json")
    assert response.status_code == 200
    assert events().count() == 1


@pytest.mark.parametrize(
    ("field", "value", "code", "response_field"),
    [
        ("event", "", "INVALID_EVENT", "event"),
        ("event", 5, "INVALID_EVENT", "event"),
        ("distinct_id", None, "INVALID_DISTINCT_ID", "distinct_id"),
        ("uuid", "bad", "INVALID_UUID", "uuid"),
        ("timestamp", "bad", "INVALID_TIMESTAMP", "timestamp"),
        ("groups", [], "INVALID_GROUPS", "groups"),
        ("properties", [], "INVALID_PROPERTIES", "properties"),
        ("project_id", str(uuid4()), "UNKNOWN_ENVELOPE_FIELD", None),
    ],
)
def test_envelope_rejections(producer, event, field, value, code, response_field):
    event[field] = value
    response = producer.post("/api/v1/capture/", event, format="json")
    assert response.status_code == 400
    assert response.data["code"] == code
    assert response.data["field"] == response_field
    assert events().count() == 0


def test_bulk_partial_rejection(producer, event):
    bad = {**event, "uuid": str(uuid4()), "timestamp": "bad"}
    other = {**event, "uuid": str(uuid4()), "event": "ticket_updated"}
    response = producer.post("/api/v1/bulk/", {"events": [event, bad, other, None]}, format="json")
    assert response.status_code == 200
    assert response.data["accepted"] == response.data["rejected"] == 2
    assert len(response.data["results"]) == 4
    assert response.data["results"][1]["code"] == "INVALID_TIMESTAMP"
    assert events().count() == 2


@pytest.mark.parametrize("payload", [[], {}, {"events": {}}, {"events": []}])
def test_invalid_bulk_envelope(producer, payload):
    response = producer.post("/api/v1/bulk/", payload, format="json")
    assert response.status_code == 400
    assert events().count() == 0


def test_bad_json(producer):
    response = producer.post("/api/v1/bulk/", "{", content_type="application/json")
    assert response.status_code == 400
    assert response.data["code"] == "INVALID_JSON"


@pytest.mark.parametrize("auth", ["", "Bearer invalid", "Basic secret"])
def test_invalid_authentication(auth, event):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=auth)
    assert client.post("/api/v1/capture/", event, format="json").status_code == 401


@pytest.mark.parametrize("scope", ["project", "workspace"])
def test_inactive_scope(producer, event, project, scope):
    row = project if scope == "project" else project.workspace
    row.is_active = False
    row.save()
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 401
    assert events().count() == 0


def test_revocation_and_rotation(staff, project, credential, producer, event):
    url = f"/api/v1/projects/{project.pk}/ingestion-credentials/{credential['id']}/"
    rotated = staff.post(url + "rotate/")
    assert rotated.status_code == 201
    assert rotated.data["secret"] != credential["secret"]
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    assert staff.post(url + "revoke/").status_code == 200
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 401
    assert staff.patch(url, {"revoked_at": None}, format="json").status_code == 404
    producer.credentials(HTTP_AUTHORIZATION=f"Bearer {rotated.data['secret']}")
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 200


def test_credential_contract_has_no_event_requirement_switches(staff, project):
    response = staff.post(
        f"/api/v1/projects/{project.pk}/ingestion-credentials/",
        {"name": "integration"},
        format="json",
    )
    assert response.status_code == 201
    assert "require_product" not in response.data
    assert "require_account" not in response.data


@pytest.mark.parametrize("removed_field", ["require_product", "require_account"])
def test_credential_rejects_removed_requirement_switches(staff, project, removed_field):
    response = staff.post(
        f"/api/v1/projects/{project.pk}/ingestion-credentials/",
        {"name": "integration", removed_field: True},
        format="json",
    )
    assert response.status_code == 400


def test_existing_definition_metadata_preserved(producer, project, event):
    definition = project.event_definitions.create(
        product_key="helpdesk",
        name=event["event"],
        status="hidden",
        owner="team",
        description="keep",
    )
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    definition.refresh_from_db()
    assert (definition.status, definition.owner, definition.description) == (
        "hidden",
        "team",
        "keep",
    )
    assert definition.last_seen_at.isoformat() == "2026-09-17T10:30:00+00:00"


def test_event_definition_last_seen_tracks_latest_occurrence(producer, project, event):
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    definition = project.event_definitions.get()
    assert definition.last_seen_at.isoformat() == "2026-09-17T10:30:00+00:00"

    later = {
        **event,
        "uuid": str(uuid4()),
        "timestamp": "2026-09-20T10:30:00Z",
    }
    assert producer.post("/api/v1/capture/", later, format="json").status_code == 201

    delayed = {
        **event,
        "uuid": str(uuid4()),
        "timestamp": "2026-09-10T10:30:00Z",
    }
    assert producer.post("/api/v1/capture/", delayed, format="json").status_code == 201

    definition.refresh_from_db()
    assert definition.last_seen_at.isoformat() == "2026-09-20T10:30:00+00:00"


def test_same_event_name_discovers_separate_product_definitions(producer, event, project):
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    bi_event = {
        **event,
        "uuid": str(uuid4()),
        "properties": {**event["properties"], "product": "bi"},
    }

    assert producer.post("/api/v1/capture/", bi_event, format="json").status_code == 201

    assert set(
        project.event_definitions.values_list("product_key", "name")
    ) == {
        ("helpdesk", "ticket_created"),
        ("bi", "ticket_created"),
    }


@pytest.mark.parametrize("product", ["", " helpdesk", 42, None, ["helpdesk"], "x" * 81])
def test_product_classification_must_be_a_valid_key(producer, event, product):
    event["properties"]["product"] = product

    response = producer.post("/api/v1/capture/", event, format="json")

    assert response.status_code == 400
    assert response.data["code"] == "INVALID_PROPERTIES"
    assert response.data["field"] == "properties.product"
    assert events().count() == 0


def test_group_count_is_bounded(producer, event, settings):
    settings.INGESTION_MAX_GROUPS = 1
    event["groups"] = {"account": "acme", "team": "support"}

    response = producer.post("/api/v1/capture/", event, format="json")

    assert response.status_code == 400
    assert response.data["code"] == "INVALID_GROUPS"
    assert response.data["field"] == "groups"
    assert events().count() == 0


@pytest.mark.parametrize(
    ("properties", "field"),
    [
        ({"$group_type": "account"}, "properties.$group_key"),
        (
            {"$group_type": "account", "$group_key": "acme"},
            "properties.$group_set",
        ),
        (
            {"$group_type": "", "$group_key": "acme", "$group_set": {}},
            "properties.$group_type",
        ),
        (
            {"$group_type": "account", "$group_key": 12, "$group_set": {}},
            "properties.$group_key",
        ),
        (
            {"$group_type": "account", "$group_key": "acme", "$group_set": []},
            "properties.$group_set",
        ),
    ],
)
def test_groupidentify_requires_valid_group_metadata(producer, event, properties, field):
    event.update(event="$groupidentify", groups={}, properties=properties)

    response = producer.post("/api/v1/capture/", event, format="json")

    assert response.status_code == 400
    assert response.data["code"] == "INVALID_GROUP_IDENTIFY"
    assert response.data["field"] == field
    assert events().count() == 0


def test_grouped_event_creates_empty_profile(producer, event, project):
    response = producer.post("/api/v1/capture/", event, format="json")

    assert response.status_code == 201
    profile = project.group_profiles.get(group_type="account", group_key="acme")
    assert profile.properties == {}
    assert profile.last_seen_at.isoformat() == "2026-09-17T10:30:00+00:00"


def test_groupidentify_updates_profile_without_catalog_definition(producer, event, project):
    event.update(
        event="$groupidentify",
        groups={},
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"name": "Acme", "plan": "enterprise"},
        },
    )

    response = producer.post("/api/v1/capture/", event, format="json")

    assert response.status_code == 201
    assert project.group_profiles.get().properties == {
        "name": "Acme",
        "plan": "enterprise",
    }
    assert project.event_definitions.count() == 0
    assert events().get().event == "$groupidentify"


def test_group_identification_does_not_retroactively_associate_events(
    producer, event, project
):
    event["groups"] = {}
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    stored_id = events().get().pk
    identify = {
        **event,
        "uuid": str(uuid4()),
        "event": "$groupidentify",
        "properties": {
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"name": "Acme"},
        },
    }

    assert producer.post("/api/v1/capture/", identify, format="json").status_code == 201

    assert events().get(pk=stored_id).groups == {}
    assert project.group_profiles.get().properties == {"name": "Acme"}


def test_groupidentify_retry_and_conflict_do_not_repeat_or_leak_updates(
    producer, event, project
):
    event.update(
        event="$groupidentify",
        groups={},
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"plan": "trial"},
        },
    )
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201

    retry = producer.post("/api/v1/capture/", event, format="json")
    assert retry.status_code == 200
    assert retry.data["duplicate"] is True

    event["properties"]["$group_set"] = {"plan": "enterprise", "leaked": True}
    conflict = producer.post("/api/v1/capture/", event, format="json")

    assert conflict.status_code == 409
    assert conflict.data["code"] == "UUID_CONFLICT"
    assert project.group_profiles.get().properties == {"plan": "trial"}
    assert project.event_definitions.count() == 0
    assert events().count() == 1


def test_limits(producer, event, settings):
    settings.INGESTION_MAX_BATCH_EVENTS = 1
    response = producer.post("/api/v1/bulk/", {"events": [event, event]}, format="json")
    assert response.status_code == 413
    settings.INGESTION_MAX_PROPERTY_BYTES = 10
    response = producer.post("/api/v1/capture/", event, format="json")
    assert response.data["code"] == "PROPERTIES_TOO_LARGE"
    settings.INGESTION_MAX_REQUEST_BYTES = 10
    response = producer.post("/api/v1/capture/", event, format="json")
    assert response.status_code == 413
    assert events().count() == 0


def test_rejection_logs_are_sanitized(producer, event, caplog, credential):
    event.update(timestamp="secret-password", event="Bearer secret-token")
    event["properties"]["password"] = "super-secret"
    with caplog.at_level("INFO", logger="analytics.ingestion"):
        response = producer.post("/api/v1/capture/", event, format="json")
    assert response.status_code == 400
    records = [r for r in caplog.records if r.name == "analytics.ingestion"]
    assert records
    output = json.dumps([r.__dict__ for r in records], default=str)
    for secret in ["secret-password", "secret-token", "super-secret", credential["secret"]]:
        assert secret not in output
    assert "INVALID_TIMESTAMP" in output
    assert response["X-Request-ID"] in output


def test_credentials_are_staff_only(project, producer, django_user_model):
    url = f"/api/v1/projects/{project.pk}/ingestion-credentials/"
    assert APIClient().post(url, {"name": "bad"}, format="json").status_code == 401
    client = APIClient()
    client.force_authenticate(django_user_model.objects.create_user(username="member"))
    assert client.post(url, {"name": "bad"}, format="json").status_code == 403
    assert producer.post(url, {"name": "bad"}, format="json").status_code == 401


def test_project_and_workspace_isolation(staff, project, producer, credential, event):
    from analytics_platform.catalog.models import Project, Workspace

    other = Project.objects.create(
        workspace=Workspace.objects.create(key="other", name="Other"), key="main", name="Other"
    )
    response = staff.post(
        f"/api/v1/projects/{other.pk}/ingestion-credentials/", {"name": "other"}, format="json"
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['secret']}")
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    assert client.post("/api/v1/capture/", event, format="json").status_code == 201
    assert events().filter(uuid=event["uuid"]).count() == 2
    assert other.event_definitions.count() == project.event_definitions.count() == 1
    url = f"/api/v1/projects/{other.pk}/ingestion-credentials/{credential['id']}/revoke/"
    assert staff.post(url).status_code == 404


def test_group_profiles_are_isolated_by_project(staff, project, producer, event):
    from analytics_platform.catalog.models import Project

    other = Project.objects.create(workspace=project.workspace, key="other", name="Other")
    response = staff.post(
        f"/api/v1/projects/{other.pk}/ingestion-credentials/",
        {"name": "other"},
        format="json",
    )
    other_producer = APIClient()
    other_producer.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['secret']}")
    first = {
        **event,
        "event": "$groupidentify",
        "groups": {},
        "properties": {
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"plan": "enterprise"},
        },
    }
    second = {
        **first,
        "properties": {
            **first["properties"],
            "$group_set": {"plan": "trial"},
        },
    }

    assert producer.post("/api/v1/capture/", first, format="json").status_code == 201
    assert other_producer.post("/api/v1/capture/", second, format="json").status_code == 201

    assert project.group_profiles.get().properties == {"plan": "enterprise"}
    assert other.group_profiles.get().properties == {"plan": "trial"}


def test_bulk_group_operations_keep_valid_neighbors(producer, event, project):
    identify = {
        **event,
        "event": "$groupidentify",
        "groups": {},
        "properties": {
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"plan": "trial"},
        },
    }
    malformed = {
        **identify,
        "uuid": str(uuid4()),
        "properties": {
            "$group_type": "company",
            "$group_key": "must-not-exist",
        },
    }
    grouped = {
        **event,
        "uuid": str(uuid4()),
        "groups": {"team": "support"},
    }

    response = producer.post(
        "/api/v1/bulk/",
        {"events": [identify, malformed, grouped]},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["accepted"] == 2
    assert response.data["rejected"] == 1
    assert [result["status"] for result in response.data["results"]] == [
        "accepted",
        "rejected",
        "accepted",
    ]
    assert not project.group_profiles.filter(group_type="company").exists()
    assert set(project.group_profiles.values_list("group_type", "group_key")) == {
        ("account", "acme"),
        ("team", "support"),
    }


def test_group_profile_update_rolls_back_when_event_storage_fails(
    producer, event, project, monkeypatch
):
    from django.db import DatabaseError

    from analytics_platform.events.models import Event

    project.group_profiles.create(
        group_type="account",
        group_key="acme",
        properties={"plan": "trial"},
    )
    event.update(
        event="$groupidentify",
        groups={},
        properties={
            "$group_type": "account",
            "$group_key": "acme",
            "$group_set": {"plan": "enterprise"},
        },
    )

    def fail_save(*args, **kwargs):
        raise DatabaseError("sensitive database failure")

    monkeypatch.setattr(Event, "save", fail_save)

    response = producer.post("/api/v1/capture/", event, format="json")

    assert response.status_code == 503
    assert response.data["code"] == "STORAGE_UNAVAILABLE"
    assert events().count() == 0
    profile = project.group_profiles.get(group_type="account", group_key="acme")
    assert profile.properties == {"plan": "trial"}


def test_concurrent_groupidentify_events_merge_properties(project, event):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connection, connections

    from analytics_platform.ingestion.credentials import create_credential
    from analytics_platform.ingestion.service import ingest_event

    assert connection.vendor == "postgresql", "Concurrency tests require PostgreSQL"
    credential, _ = create_credential(project, name="test")

    def submit(property_name, property_value):
        close_old_connections()
        payload = {
            **event,
            "uuid": str(uuid4()),
            "event": "$groupidentify",
            "groups": {},
            "properties": {
                "$group_type": "account",
                "$group_key": "acme",
                "$group_set": {property_name: property_value},
            },
        }
        try:
            return ingest_event(credential, payload, request_id=str(uuid4()))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit, "plan", "enterprise")
        second = pool.submit(submit, "region", "india")
        responses = [first.result(timeout=15), second.result(timeout=15)]

    assert [status for _, status in responses] == [201, 201]
    assert project.group_profiles.get().properties == {
        "plan": "enterprise",
        "region": "india",
    }
    assert events().filter(event="$groupidentify").count() == 2


def test_wrong_secret_with_real_prefix(producer, credential, event):
    producer.credentials(HTTP_AUTHORIZATION=f"Bearer {credential['prefix']}.wrong-secret")
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 401


def test_service_rejects_revoked_credential(project, event):
    from analytics_platform.ingestion.credentials import create_credential, revoke_credential
    from analytics_platform.ingestion.service import ingest_event

    credential, _ = create_credential(project, name="test")
    revoke_credential(credential)
    result, status = ingest_event(credential, event, request_id=str(uuid4()))
    assert status == 401
    assert result["code"] == "INVALID_CREDENTIAL"
    assert events().count() == 0


def test_event_cannot_be_updated_but_can_be_deleted(producer, event):
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    row = events().get()
    row.event = "changed"
    with pytest.raises(TypeError):
        row.save()
    with pytest.raises(TypeError):
        events().update(event="changed")
    with pytest.raises(TypeError):
        events().bulk_update([row], ["event"])
    with pytest.raises(TypeError):
        events().bulk_create(
            [row], update_conflicts=True, update_fields=["event"], unique_fields=["project", "uuid"]
        )
    assert events().get().event == event["event"]
    row.delete()
    assert events().count() == 0


def test_failed_commit_never_reports_accepted(project, event, monkeypatch):
    from django.db import DatabaseError, connection

    from analytics_platform.ingestion.credentials import create_credential
    from analytics_platform.ingestion.service import ingest_event

    credential, _ = create_credential(project, name="test")

    def fail_commit():
        raise DatabaseError("sensitive database failure")

    with monkeypatch.context() as patch:
        patch.setattr(connection, "commit", fail_commit)
        result, status = ingest_event(credential, event, request_id=str(uuid4()))
    assert status == 503
    assert result["code"] == "STORAGE_UNAVAILABLE"
    assert events().count() == 0
    assert project.event_definitions.count() == 0


def test_service_cannot_report_success_inside_outer_transaction(project, event):
    from django.db import transaction

    from analytics_platform.ingestion.credentials import create_credential
    from analytics_platform.ingestion.service import ingest_event

    credential, _ = create_credential(project, name="test")
    with transaction.atomic(), pytest.raises(RuntimeError, match="durable"):
        ingest_event(credential, event, request_id=str(uuid4()))
    assert events().count() == 0


@pytest.mark.parametrize("mode", ["discovery", "duplicate", "conflict"])
def test_concurrent_requests(project, event, mode, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection, connections

    from analytics_platform.event_catalog.models import EventDefinition
    from analytics_platform.ingestion.credentials import create_credential
    from analytics_platform.ingestion.service import ingest_event

    assert connection.vendor == "postgresql", "Concurrency tests require PostgreSQL"
    # This test synchronizes event-definition discovery. Grouped events for the
    # same profile are intentionally serialized by the profile row lock.
    event["groups"] = {}
    credential, _ = create_credential(project, name="first")
    second, _ = create_credential(project, name="second")
    other = {**event}
    if mode == "discovery":
        other["uuid"] = str(uuid4())
    if mode == "conflict":
        other["distinct_id"] = "helpdesk:agent:99"
    barrier = Barrier(2)
    original = EventDefinition.objects.get_or_create

    def synchronized_discovery(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(EventDefinition.objects, "get_or_create", synchronized_discovery)

    def submit(credential, payload):
        close_old_connections()
        try:
            return ingest_event(credential, payload, request_id=str(uuid4()))
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit, credential, event)
        second = pool.submit(submit, second, other)
        responses = [first.result(timeout=15), second.result(timeout=15)]
    assert project.event_definitions.count() == 1
    assert project.event_definitions.get().status == "visible"
    if mode == "discovery":
        assert sorted(status for _, status in responses) == [201, 201]
        assert events().count() == 2
    elif mode == "duplicate":
        assert sorted(status for _, status in responses) == [200, 201]
        assert events().count() == 1
    else:
        assert sorted(status for _, status in responses) == [201, 409]
        assert events().count() == 1


def test_bulk_acceptance_is_visible_to_another_connection(producer, event):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connections

    response = producer.post("/api/v1/bulk/", {"events": [event, None]}, format="json")
    assert response.data["accepted"] == 1

    def read_committed():
        try:
            return events().filter(uuid=event["uuid"]).exists()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(read_committed).result(timeout=10)


@pytest.mark.parametrize("value", ["\x00", "\ud800", float("inf")])
def test_postgresql_incompatible_properties_rejected(producer, event, value):
    event["properties"]["bad"] = value
    payload = json.dumps(event)
    response = producer.post("/api/v1/capture/", payload, content_type="application/json")
    assert response.status_code == 400
    assert events().count() == 0


def test_unknown_envelope_field_does_not_leak_into_logs(producer, event, caplog):
    event["secret-password"] = "sensitive"
    with caplog.at_level("INFO", logger="analytics.ingestion"):
        response = producer.post("/api/v1/capture/", event, format="json")
    assert response.status_code == 400
    assert response.data["code"] == "UNKNOWN_ENVELOPE_FIELD"
    assert "secret-password" not in caplog.text


@pytest.mark.parametrize("number", [1e23, 1e-7])
def test_identical_numeric_properties_retry_after_postgres_normalization(producer, event, number):
    event["properties"]["number"] = number
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 200


def test_boolean_and_number_are_conflicting_event_data(producer, event):
    event["properties"]["value"] = True
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    event["properties"]["value"] = 1
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 409


def test_literal_escaped_unicode_is_valid_property(producer, event):
    event["properties"]["literal"] = r"\u0000"
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201


def test_bulk_keeps_committed_success_when_another_write_fails(producer, event, monkeypatch):
    from django.db import DatabaseError

    from analytics_platform.events.models import Event

    original = Event.save

    def fail_second(self, *args, **kwargs):
        if self.event == "fail_storage":
            raise DatabaseError("password=should-not-appear")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Event, "save", fail_second)
    bad = {**event, "event": "fail_storage", "uuid": str(uuid4())}
    last = {**event, "event": "last_event", "uuid": str(uuid4())}
    response = producer.post("/api/v1/bulk/", {"events": [event, bad, last]}, format="json")
    assert response.data["accepted"] == 2
    assert response.data["rejected"] == 1
    assert response.data["results"][1]["code"] == "STORAGE_UNAVAILABLE"
    assert set(events().values_list("event", flat=True)) == {"ticket_created", "last_event"}


def test_timestamp_offset_and_json_key_order_do_not_conflict(producer, event):
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    event["timestamp"] = "2026-09-17T16:00:00+05:30"
    event["properties"] = dict(reversed(list(event["properties"].items())))
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 200


def test_deep_json_duplicate_does_not_abort_bulk(producer, event):
    nested = "leaf"
    for _ in range(500):
        nested = [nested]
    event["properties"]["nested"] = nested
    assert producer.post("/api/v1/capture/", event, format="json").status_code == 201
    following = {**event, "uuid": str(uuid4()), "properties": {"product": "helpdesk"}}
    response = producer.post("/api/v1/bulk/", {"events": [event, following]}, format="json")
    assert response.status_code == 200
    assert response.data["accepted"] == 2
    assert response.data["results"][0]["duplicate"] is True
    assert events().count() == 2


@pytest.mark.parametrize("value", [1, True, None, [], {}])
def test_uuid_requires_valid_string(producer, event, value):
    event["uuid"] = value
    response = producer.post("/api/v1/capture/", event, format="json")
    assert response.status_code == 400
    assert response.data["code"] == "INVALID_UUID"


def test_rotation_preserves_credential_name_without_policy_switches(staff, project):
    url = f"/api/v1/projects/{project.pk}/ingestion-credentials/"
    response = staff.post(url, {"name": "HappyFox"}, format="json")
    assert response.status_code == 201
    rotated = staff.post(url + response.data["id"] + "/rotate/")
    assert rotated.status_code == 201
    assert rotated.data["name"] == "HappyFox"
    assert "require_product" not in rotated.data
    assert "require_account" not in rotated.data
    assert rotated.data["revoked_at"] is None
