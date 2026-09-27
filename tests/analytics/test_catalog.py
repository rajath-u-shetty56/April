from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from analytics_platform.analytics.catalog import describe_project, get_account_profile
from analytics_platform.analytics.contracts import AnalyticsInputError
from analytics_platform.catalog.models import Project
from analytics_platform.event_catalog.models import EventDefinition
from analytics_platform.events.models import Event
from analytics_platform.group_analytics.models import GroupProfile

pytestmark = pytest.mark.django_db


def test_describe_project_is_scoped_and_bounds_ordered_definitions(project, workspace):
    other = Project.objects.create(workspace=workspace, key="other", name="Other")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for offset, product, name in [
        (2, "rise", "course_completed"),
        (0, "contact_center", "call_connected"),
        (1, "helpdesk", "macro_applied"),
    ]:
        EventDefinition.objects.create(
            project=project,
            product_key=product,
            name=name,
            last_seen_at=start + timedelta(days=offset),
        )
        Event.objects.create(
            project=project,
            uuid=uuid4(),
            event=name,
            distinct_id=f"user:{offset}",
            timestamp=start + timedelta(days=offset),
            groups={"account": "acme"},
            properties={"product": product},
        )
    EventDefinition.objects.create(project=other, product_key="bi", name="secret_event")
    Event.objects.create(
        project=other,
        uuid=uuid4(),
        event="secret_event",
        distinct_id="secret-user",
        timestamp=start - timedelta(days=10),
        groups={"account": "other"},
        properties={"product": "bi"},
    )
    GroupProfile.objects.create(
        project=project,
        group_type="account",
        group_key="acme",
        properties={"region": "IN"},
        last_seen_at=start,
    )
    GroupProfile.objects.create(
        project=other,
        group_type="account",
        group_key="other",
        properties={"secret": True},
    )

    result = describe_project(project, event_definition_limit=2)
    payload = result.to_dict()

    assert payload["scope"] == {"project_id": str(project.pk)}
    assert payload["counts"] == {"events": 3, "group_profiles": 1, "event_definitions": 3}
    assert payload["products"] == ["contact_center", "helpdesk", "rise"]
    assert payload["event_range"] == {
        "earliest": "2026-09-01T00:00:00Z",
        "latest": "2026-09-03T00:00:00Z",
    }
    assert [item["name"] for item in payload["event_definitions"]["items"]] == [
        "call_connected",
        "macro_applied",
    ]
    assert payload["event_definitions"] | {"items": []} == {
        "items": [],
        "returned_count": 2,
        "total_count": 3,
        "truncated": True,
    }


def test_get_account_profile_returns_current_state_or_empty(project):
    seen_at = datetime(2026, 9, 1, tzinfo=UTC)
    GroupProfile.objects.create(
        project=project,
        group_type="account",
        group_key="acme",
        properties={"contact_center_status": "active"},
        last_seen_at=seen_at,
    )

    found = get_account_profile(project, "acme").to_dict()
    missing = get_account_profile(project, "missing").to_dict()

    assert found == {
        "scope": {"project_id": str(project.pk)},
        "account_key": "acme",
        "found": True,
        "state_basis": "current_profile",
        "properties": {"contact_center_status": "active"},
        "last_seen_at": "2026-09-01T00:00:00Z",
    }
    assert missing["found"] is False
    assert missing["properties"] == {}


@pytest.mark.parametrize("account_key", ["", "   ", "x" * 401])
def test_get_account_profile_rejects_invalid_account_keys(project, account_key):
    with pytest.raises(AnalyticsInputError, match="account_key"):
        get_account_profile(project, account_key)
